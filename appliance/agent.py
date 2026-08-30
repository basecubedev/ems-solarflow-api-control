# SPDX-License-Identifier: AGPL-3.0-or-later
"""The privileged host agent.

The agent is the only process with host privileges. It listens on a local Unix
socket that is never reachable over the network, checks the peer's credentials,
re-validates every request against the fixed operation allowlist and refuses
anything else. It accepts no command string, no path and no image reference.
"""

import contextlib
import json
import os
import pwd
import shutil
import socket
import socketserver
import struct
import threading

from appliance import (
    admin_bootstrap,
    admin_deployment,
    admin_lifecycle,
    commands,
    config as appliance_config,
    docker_backend,
    host_config,
    install_check,
    manager_install,
    manager_releases,
    manager_update,
    manager_verify,
    network,
    operation_schema,
    artifact_trust,
    packages,
    persistent_state,
    releases,
    rescue_account,
    ssh_service,
    auth,
    support_archive,
    systemd,
    timezone_config,
    validation,
)
from appliance.audit import RESULT_DENIED, RESULT_FAILURE, RESULT_SUCCESS
from appliance.redaction import redact_text
from appliance.release_fetch import FetchError
from appliance.operations import (
    STATE_FAILED_RECOVERABLE,
    STATE_FAILED_TERMINAL,
    OperationConflictError,
    OperationError,
)
from appliance.paths import AGENT_SOCKET_NAME
from appliance.protocol import OPERATIONS, ProtocolError, ValidationContext, validate_request
from appliance.validation import ValidationError

MAX_REQUEST_BYTES = 64 * 1024
SOCKET_MODE = 0o660

PLAN_TYPES = {
    "admin.plan_install": admin_lifecycle.TYPE_INSTALL,
    "admin.plan_rollback": admin_lifecycle.TYPE_ROLLBACK,
    "admin.plan_repair": admin_lifecycle.TYPE_REPAIR,
    "admin.plan_lifecycle": admin_lifecycle.TYPE_LIFECYCLE,
    "updates.plan": packages.TYPE_UPDATE_INSTALL,
    "updates.plan_repair": packages.TYPE_UPDATE_REPAIR,
    "network.wifi.plan": network.TYPE_WIFI,
    "network.hostname.plan": network.TYPE_HOSTNAME,
    "system.timezone.plan": timezone_config.TYPE_TIMEZONE,
    "ssh.plan_service": ssh_service.TYPE_SSH_SERVICE,
    "ssh.plan_key_add": ssh_service.TYPE_SSH_KEY_ADD,
    "ssh.plan_key_remove": ssh_service.TYPE_SSH_KEY_REMOVE,
    "ssh.plan_revoke_all": ssh_service.TYPE_SSH_REVOKE_ALL,
    "support.plan_archive": support_archive.TYPE_SUPPORT_ARCHIVE,
    "manager.plan_update": manager_update.TYPE_MANAGER_UPDATE,
    "manager.plan_revert": manager_update.TYPE_MANAGER_REVERT,
    "system.plan_reboot": "system.reboot",
    "system.plan_shutdown": "system.shutdown",
}

AUDITED_PLANS = {
    admin_lifecycle.TYPE_INSTALL: "admin.install",
    admin_lifecycle.TYPE_ROLLBACK: "admin.rollback",
    admin_lifecycle.TYPE_REPAIR: "admin.repair",
    packages.TYPE_UPDATE_INSTALL: "updates.install",
    packages.TYPE_UPDATE_REPAIR: "updates.repair",
    network.TYPE_WIFI: "network.wifi",
    network.TYPE_HOSTNAME: "network.hostname",
    timezone_config.TYPE_TIMEZONE: "system.timezone",
    ssh_service.TYPE_SSH_KEY_ADD: "ssh.key_added",
    ssh_service.TYPE_SSH_KEY_REMOVE: "ssh.key_removed",
    ssh_service.TYPE_SSH_REVOKE_ALL: "ssh.keys_revoked",
    "system.reboot": "system.reboot",
    "system.shutdown": "system.shutdown",
    manager_update.TYPE_MANAGER_UPDATE: "manager.update.plan",
    manager_update.TYPE_MANAGER_REVERT: "manager.revert.plan",
}


class AgentError(Exception):
    def __init__(self, code, message, *, field=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def _thread_executor(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _inline_executor(target):
    target()
    return None


# The one list of errors a service may raise that are a refusal rather than a
# defect. Both the socket server and the in-process client answer on it, so it
# lives here rather than being spelled out twice.
# Every typed service error whose code and message are written for an operator
# to read. The list is an allowlist on purpose: anything not named here reaches
# the browser as the bare class name, which is how "DockerError" became the
# whole account of why a log could not be read.
SERVICE_ERRORS = (
    AgentError,
    ValidationError,
    OperationError,
    admin_bootstrap.BootstrapError,
    admin_deployment.DeploymentError,
    admin_lifecycle.AdminLifecycleError,
    appliance_config.ConfigError,
    artifact_trust.ReleaseError,
    auth.AuthError,
    commands.CommandError,
    docker_backend.DockerError,
    host_config.HostConfigError,
    manager_install.ManagerInstallError,
    manager_releases.ManagerReleaseError,
    manager_update.ManagerUpdateError,
    manager_verify.ManagerVerifyError,
    network.NetworkError,
    operation_schema.OperationSchemaError,
    packages.PackageError,
    persistent_state.PersistentStateError,
    releases.ReleaseResolutionError,
    rescue_account.RescueAccountError,
    ssh_service.SshServiceError,
    support_archive.SupportArchiveError,
    systemd.SystemdError,
    timezone_config.TimezoneError,
    FetchError,
)


class AgentHandlers:
    """Dispatch a validated request onto the privileged services."""

    def __init__(self, services, *, executor=None):
        self.services = services
        self.context = ValidationContext(services.config)
        self._execute = executor or _thread_executor

    # --- entry point -----------------------------------------------------

    def dispatch(self, payload, *, actor="", source_ip=""):
        spec, args = validate_request(payload, self.context)
        if spec.name in PLAN_TYPES:
            return self._plan(spec, args, actor=actor, source_ip=source_ip)
        if spec.name == "operations.execute":
            return self._execute_operation(args, actor=actor, source_ip=source_ip)
        if spec.name == "auth.state":
            store = self.services.auth
            return {"configured": store.configured(), "generation": store.generation()}
        if spec.name == "auth.verify":
            return {"ok": bool(self.services.auth.verify(args["password"]))}
        if spec.name == "auth.create":
            self.services.auth.create(args["password"], args.get("confirmation") or None)
            return {"generation": self.services.auth.generation()}
        if spec.name == "auth.change":
            self.services.auth.change(
                args["current_password"],
                args["password"],
                args.get("confirmation") or None,
            )
            return {"generation": self.services.auth.generation()}
        if spec.name == "support.read_archive":
            return self.services.support.read(args["operation_id"])
        if spec.name == "operations.cancel":
            record = self.services.operations.cancel(args["operation_id"])
            self.services.network.discard_secret(args["operation_id"])
            return {"operation": record.to_dict()}
        if spec.name == "operations.acknowledge":
            record = self.services.operations.acknowledge(args["operation_id"])
            return {"operation": record.to_dict()}
        if spec.name == "audit.record_web_event":
            return self._record_web_event(args, actor=actor, source_ip=source_ip)
        return self._read_only(spec, args)

    # --- audit -----------------------------------------------------------

    def _record_web_event(self, args, *, actor, source_ip):
        """The web service's only way into the audit log.

        Every field was validated against a fixed set by the protocol, so
        nothing the browser sent can widen an action name or reach a path.
        """

        entry = self.services.audit.record(
            args["event"],
            user=actor,
            source_ip=source_ip,
            target=args["reason"],
            result=args["result"],
        )
        return {
            "recorded": True,
            "event": args["event"],
            "result": args["result"],
            "timestamp": entry.get("timestamp"),
        }

    # --- read-only -------------------------------------------------------

    def _read_only(self, spec, args):
        status = self.services.status
        if spec.name == "status.get":
            return status.overview()
        if spec.name == "system.get":
            return status.system()
        if spec.name == "network.get":
            return status.network_state()
        if spec.name == "docker.get":
            return status.docker_state()
        if spec.name == "admin.get":
            return status.admin_state()
        if spec.name == "updates.get":
            return status.updates()
        if spec.name == "ssh.get":
            return status.ssh_state()
        if spec.name == "backup.get":
            return status.backup_state()
        if spec.name == "admin.releases":
            return self.services.admin.releases()
        if spec.name == "manager.status":
            return self._require_manager().status()
        if spec.name == "manager.sources":
            return self._require_manager().sources()
        if spec.name == "network.wifi.scan":
            return {"networks": self.services.network.scan()}
        if spec.name == "install.verify":
            return install_check.verify_installation(
                paths=self.services.paths, runner=self.services.runner, in_agent=True
            )
        if spec.name == "operations.list":
            return status.operations_state()
        if spec.name == "operations.get":
            record = self.services.operations.get(args["operation_id"])
            return {"operation": record.to_dict()}
        if spec.name == "logs.read":
            return status.read_log(args["source"], args["lines"])
        raise AgentError("unknown_operation", f"{spec.name} has no handler")

    # --- planning --------------------------------------------------------

    def _plan(self, spec, args, *, actor, source_ip):
        operation_type = PLAN_TYPES[spec.name]
        try:
            operation = self.services.operations.create(operation_type, actor=actor)
        except OperationConflictError as exc:
            raise AgentError(exc.code, exc.message)

        try:
            plan = self._build_plan(spec.name, operation, args)
        except (
            admin_lifecycle.AdminLifecycleError,
            packages.PackageError,
            network.NetworkError,
            ssh_service.SshServiceError,
            artifact_trust.ReleaseError,
            manager_update.ManagerUpdateError,
            FetchError,
            ValidationError,
            OperationError,
        ) as exc:
            self._abandon_plan(operation, operation_type, actor, source_ip)
            raise AgentError(getattr(exc, "code", "plan_failed"), str(getattr(exc, "message", exc)))
        except BaseException:
            # The lock this planner took outlives the exception unless it is
            # released here, and a lock nobody releases blocks every later
            # operation on the appliance.
            self._abandon_plan(operation, operation_type, actor, source_ip)
            raise

        # The planner has finished writing the target, so this is the last
        # moment the record and the plan describe the same thing. Sealing them
        # together is what lets confirmation and execution prove they are still
        # acting on the plan the operator was shown.
        operation = self.services.operations.get(operation.operation_id)
        authority = operation_schema.seal(operation, plan)
        self.services.operations.update_target(operation.operation_id, authority)
        plan = dict(plan) | {operation_schema.AUTHORITY_FIELD: authority[
            operation_schema.AUTHORITY_FIELD
        ]}

        record = self.services.operations.await_confirmation(operation.operation_id, plan)
        return {
            "operation": record.to_dict(),
            "plan": plan,
            "confirmation_token": record.confirmation_token,
        }

    def _abandon_plan(self, operation, operation_type, actor, source_ip):
        self.services.operations.cancel(operation.operation_id)
        self.services.network.discard_secret(operation.operation_id)
        self._audit(operation_type, actor, source_ip, RESULT_FAILURE, operation.operation_id)

    def _build_plan(self, name, operation, args):
        services = self.services
        if name == "admin.plan_install":
            return services.admin.plan_install(
                operation, channel=args["channel"], tag=args.get("tag"), reinstall=args["reinstall"]
            )
        if name == "admin.plan_rollback":
            return services.admin.plan_rollback(operation)
        if name == "admin.plan_repair":
            return services.admin.plan_repair(operation)
        if name == "admin.plan_lifecycle":
            return services.admin.plan_lifecycle(operation, args["action"])
        if name == "updates.plan":
            return services.packages.plan_install(operation, args["scope"])
        if name == "updates.plan_repair":
            return services.packages.plan_repair(operation, args["action"])
        if name == "network.wifi.plan":
            return services.network.plan_wifi(
                operation,
                ssid=args["ssid"],
                passphrase=args["passphrase"],
                hidden=args["hidden"],
            )
        if name == "network.hostname.plan":
            return services.network.plan_hostname(operation, args["hostname"])
        if name == "system.timezone.plan":
            return services.timezone.plan(operation, args["timezone"])
        if name == "ssh.plan_service":
            return services.ssh.plan_service(operation, args["enabled"])
        if name == "ssh.plan_key_add":
            return services.ssh.plan_key_add(
                operation, account=args["account"], public_key=args["public_key"]
            )
        if name == "ssh.plan_key_remove":
            return services.ssh.plan_key_remove(
                operation, account=args["account"], fingerprint=args["fingerprint"]
            )
        if name == "ssh.plan_revoke_all":
            return services.ssh.plan_revoke_all(operation, args["account"])
        if name == "support.plan_archive":
            return services.support.plan(operation)
        if name == "manager.plan_update":
            return self._require_manager().plan_update(operation, args["release_id"])
        if name == "manager.plan_revert":
            return self._require_manager().plan_revert(operation)
        if name in ("system.plan_reboot", "system.plan_shutdown"):
            return self._plan_power(operation, name)
        raise AgentError("unknown_operation", f"{name} has no planner")

    def _plan_power(self, operation, name):
        action = "reboot" if name == "system.plan_reboot" else "shutdown"
        status = self.services.status
        active = self.services.operations.active()
        blockers = []
        package_state = self.services.packages.check()
        if package_state.lock_state == packages.LOCK_HELD:
            blockers.append(
                {
                    "code": "package_operation_active",
                    "message": "a package installation is running; wait until it finishes",
                }
            )
        return {
            "type": f"system.{action}",
            "action": action,
            "blockers": blockers,
            "running_operation": active.to_dict() if active else None,
            "docker": status.docker_state(),
            "warning": "EMS control stops while the appliance is "
            + ("restarting." if action == "reboot" else "powered off."),
        }

    # --- execution -------------------------------------------------------

    def _execute_operation(self, args, *, actor, source_ip):
        operation_id = args["operation_id"]
        token = args["confirmation_token"]
        store = self.services.operations
        current = store.get(operation_id, include_token=True)

        # The token proves the caller saw a plan; the authority proves the plan
        # they saw is still the one on disk. A record whose target changed after
        # the confirmation was rendered is refused before anything is confirmed.
        try:
            operation_schema.validate_confirmation(
                current,
                repositories=self.services.config.images.repositories,
                architectures=self.services.config.supported_architectures,
            )
        except operation_schema.OperationSchemaError as exc:
            store.finish(
                operation_id,
                STATE_FAILED_TERMINAL,
                stage="preflight_failed",
                result={"stage": "preflight", "admin_untouched": True},
                error={"code": exc.code, "message": exc.message},
            )
            self._audit(current.type, actor, source_ip, RESULT_DENIED, operation_id)
            raise AgentError(exc.code, exc.message)

        try:
            if current.state == STATE_FAILED_RECOVERABLE:
                operation = store.retry(operation_id, token)
            else:
                operation = store.confirm(operation_id, token)
        except OperationError as exc:
            self._audit(current.type, actor, source_ip, RESULT_DENIED, operation_id)
            raise AgentError(exc.code, exc.message)

        blockers = (operation.result or {}).get("plan", {}).get("blockers") or []
        if blockers:
            store.finish(
                operation_id,
                STATE_FAILED_TERMINAL,
                stage="blocked",
                error={"code": blockers[0]["code"], "message": blockers[0]["message"]},
            )
            raise AgentError(blockers[0]["code"], blockers[0]["message"])

        self._audit(operation.type, actor, source_ip, RESULT_SUCCESS, operation_id)
        self._execute(lambda: self._run(operation, actor=actor, source_ip=source_ip))
        return {"operation": store.get(operation_id).to_dict()}

    def _run(self, operation, *, actor, source_ip):
        store = self.services.operations
        try:
            self._executor_for(operation.type)(operation)
        except Exception as exc:
            current = store.get(operation.operation_id)
            if not current.terminal:
                store.finish(
                    operation.operation_id,
                    STATE_FAILED_TERMINAL,
                    stage="failed",
                    error={
                        "code": getattr(exc, "code", "operation_failed"),
                        "message": str(getattr(exc, "message", exc)),
                    },
                )
            self._audit(
                operation.type, actor, source_ip, RESULT_FAILURE, operation.operation_id
            )
            return
        self._audit(operation.type, actor, source_ip, RESULT_SUCCESS, operation.operation_id)

    def _executor_for(self, operation_type):
        services = self.services
        if operation_type.startswith("admin."):
            return services.admin.execute
        if operation_type.startswith("updates."):
            return services.packages.execute
        if operation_type.startswith("network."):
            return services.network.execute
        if operation_type.startswith("ssh."):
            return services.ssh.execute
        if operation_type == support_archive.TYPE_SUPPORT_ARCHIVE:
            return services.support.execute
        if operation_type == timezone_config.TYPE_TIMEZONE:
            return services.timezone.execute
        if operation_type in (
            manager_update.TYPE_MANAGER_UPDATE,
            manager_update.TYPE_MANAGER_REVERT,
        ):
            return self._execute_manager
        if operation_type in ("system.reboot", "system.shutdown"):
            return self._execute_power
        raise AgentError("unknown_operation_type", f"{operation_type} is not executable")



    def _require_manager(self):
        service = getattr(self.services, "manager", None)
        if service is None:
            raise AgentError(
                "manager_unavailable",
                "this appliance has no Appliance Manager package update service",
            )
        return service

    def _execute_manager(self, operation):
        from appliance.operations import STATE_SUCCEEDED

        result = self._require_manager().execute(operation)
        self.services.operations.finish(
            operation.operation_id, STATE_SUCCEEDED, result=result, stage=result.get("stage", "")
        )
        return result


    def _execute_power(self, operation):
        from appliance.operations import STATE_SUCCEEDED

        action = "reboot" if operation.type == "system.reboot" else "shutdown"
        self.services.operations.advance(operation.operation_id, f"{action}_requested")
        result = (
            self.services.systemd.reboot() if action == "reboot" else self.services.systemd.shutdown()
        )
        payload = {"action": action, "accepted": bool(result.ok)}
        self.services.operations.finish(
            operation.operation_id,
            STATE_SUCCEEDED if result.ok else STATE_FAILED_TERMINAL,
            result=payload,
            error=None if result.ok else {"code": "power_action_failed", "message": f"{action} failed"},
        )
        return payload

    def _audit(self, operation_type, actor, source_ip, result, operation_id):
        action = AUDITED_PLANS.get(operation_type)
        if not action:
            return
        self.services.audit.record(
            action,
            user=actor,
            source_ip=source_ip,
            target=operation_type,
            result=result,
            operation_id=operation_id,
        )


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        try:
            peer = server.peer_identity(self.connection)
        except AgentError as exc:
            self._reply({"ok": False, "error": {"code": exc.code, "message": exc.message}})
            return

        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            self._reply(
                {"ok": False, "error": {"code": "request_too_large", "message": "request too large"}}
            )
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reply(
                {"ok": False, "error": {"code": "invalid_request", "message": "malformed JSON"}}
            )
            return

        actor = validation.sanitize_actor(payload.pop("actor", ""))
        source_ip = validation.sanitize_source_ip(payload.pop("source_ip", ""))
        self._reply(server.handle_request_payload(payload, actor=actor, source_ip=source_ip, peer=peer))

    def _reply(self, payload):
        try:
            self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass


class AgentServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, services, *, socket_path=None, handlers=None, allowed_uids=None):
        self.services = services
        self.handlers = handlers or AgentHandlers(services)
        self.socket_path = str(socket_path or services.paths.agent_socket)
        self.allowed_uids = frozenset(
            allowed_uids if allowed_uids is not None else default_allowed_uids(services.config)
        )
        parent = os.path.dirname(self.socket_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        super().__init__(self.socket_path, _RequestHandler)
        os.chmod(self.socket_path, SOCKET_MODE)
        # The socket is the whole privilege boundary: only root and the web
        # service group may reach it, and it is never a network listener.
        with contextlib.suppress(LookupError, PermissionError, OSError):
            shutil.chown(self.socket_path, group=services.config.socket_group)

    def peer_identity(self, connection):
        """Refuse any local peer that is not root or the web service account."""

        try:
            raw = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            pid, uid, gid = struct.unpack("3i", raw)
        except (OSError, AttributeError):
            raise AgentError("peer_identity_unavailable", "peer credentials are unavailable")
        if uid not in self.allowed_uids:
            raise AgentError("peer_not_allowed", "this local user may not use the appliance agent")
        return {"pid": pid, "uid": uid, "gid": gid}

    def handle_request_payload(self, payload, *, actor="", source_ip="", peer=None):
        try:
            result = self.handlers.dispatch(payload, actor=actor, source_ip=source_ip)
        except ProtocolError as exc:
            return {
                "ok": False,
                "error": {"code": exc.code, "message": exc.message, "field": exc.field},
            }
        except SERVICE_ERRORS as exc:
            return {
                "ok": False,
                "error": {
                    "code": getattr(exc, "code", "operation_failed"),
                    "message": redact_text(str(getattr(exc, "message", exc))),
                },
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": {"code": "agent_error", "message": exc.__class__.__name__},
            }
        return {"ok": True, "result": result}

    def server_close(self):
        super().server_close()
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


def default_allowed_uids(config):
    uids = {0}
    try:
        uids.add(pwd.getpwnam(config.web_user).pw_uid)
    except KeyError:
        pass
    return uids


def operation_names():
    return sorted(OPERATIONS)


def socket_path_for(paths):
    return paths.runtime_dir / AGENT_SOCKET_NAME
