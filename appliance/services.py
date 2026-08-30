# SPDX-License-Identifier: AGPL-3.0-or-later
"""Construct the appliance service graph once, for the agent and the CLI.

Everything privileged is built here, so the unprivileged web process never
imports a service that could touch Docker, apt, systemd or ``authorized_keys``.
"""

from dataclasses import dataclass

from appliance.admin_lifecycle import AdminLifecycleService
from appliance.audit import AuditLog, OperationLog
from appliance.backup_access import BackupAccessService
from appliance.commands import CommandRunner
from appliance.config import load_config
from appliance.docker_backend import DockerBackend
from appliance.health import HttpHealthChecker
from appliance.hostprobe import HostProbe, host_architecture
from appliance.known_good import KnownGoodStore
from appliance.auth import AuthStore, deployment_owner
from appliance.network import NetworkService
from appliance.timezone_config import TimezoneService
from appliance.operations import OperationStore
from appliance import persistent_state
from appliance.manager_update import ManagerUpdateService
from appliance.artifact_trust import SignatureVerifier
from appliance.packages import PackageService
from appliance.paths import ensure_directories, resolve_paths
from appliance.releases import ReleaseCatalogue
from appliance.ssh_service import SshService
from appliance.status import StatusService
from appliance.support_archive import SupportArchiveService
from appliance.systemd import SystemdBackend


@dataclass
class ApplianceServices:
    paths: object
    config: object
    runner: object
    probe: object
    docker: object
    systemd: object
    operations: object
    known_good: object
    admin: object
    packages: object
    network: object
    ssh: object
    backup: object
    support: object
    timezone: object
    auth: object
    status: object
    audit: object
    operation_log: object
    manager: object = None


def build_services(
    *,
    paths=None,
    config=None,
    runner=None,
    root="/",
    health=None,
    catalogue=None,
    time_fn=None,
    sleep=None,
    create_directories=True,
    admin_bootstrap=None,
    installed_version=None,
):
    paths = paths or resolve_paths()
    if create_directories:
        ensure_directories(paths, role="agent")
    config = config or load_config(paths)
    runner = runner or CommandRunner()
    probe = HostProbe(runner, root=root, time_fn=time_fn)

    docker = DockerBackend(runner)

    systemd = SystemdBackend(runner)
    operations = OperationStore(paths.operations_dir, time_fn=time_fn)
    known_good = KnownGoodStore(paths.known_good_dir, time_fn=time_fn)
    audit = AuditLog(paths.audit_log, time_fn=time_fn)
    operation_log = OperationLog(paths.operations_log, time_fn=time_fn)

    admin = AdminLifecycleService(
        paths=paths,
        config=config,
        docker=docker,
        known_good=known_good,
        health=health or HttpHealthChecker(sleep=sleep),
        operations=operations,
        runner=runner,
        systemd=systemd,
        catalogue=catalogue or ReleaseCatalogue(config),
        time_fn=time_fn,
        sleep=sleep,
        operation_log=operation_log,
        bootstrap=admin_bootstrap,
    )
    packages = PackageService(
        runner=runner,
        probe=probe,
        paths=paths,
        config=config,
        operations=operations,
        time_fn=time_fn,
        operation_log=operation_log,
    )
    network = NetworkService(
        runner=runner,
        probe=probe,
        config=config,
        operations=operations,
        time_fn=time_fn,
        sleep=sleep,
        operation_log=operation_log,
        revert_intent_dir=paths.recovery_dir,
    )
    timezone = TimezoneService(paths=paths, config=config, operations=operations)
    auth = AuthStore(paths.auth_file, owner=deployment_owner(paths.install_root))
    ssh = SshService(
        runner=runner,
        systemd=systemd,
        config=config,
        operations=operations,
        paths=paths,
        time_fn=time_fn,
        operation_log=operation_log,
    )
    backup = BackupAccessService(paths=paths, config=config, ssh_service=ssh, probe=probe)
    # One lookup, handed to everything that reports it. dpkg is asked once per
    # process because installing a Manager restarts the process asking, and it
    # is asked at all only when a caller did not already know -- a test builds
    # these services on a host whose dpkg knows nothing about this package.
    if installed_version is None:
        from appliance.version import installed_version as resolve_installed_version

        manager_version = resolve_installed_version()
    else:
        manager_version = installed_version
    status = StatusService(
        paths=paths,
        config=config,
        probe=probe,
        docker=docker,
        systemd=systemd,
        admin=admin,
        packages=packages,
        network=network,
        ssh=ssh,
        backup=backup,
        operations=operations,
        time_fn=time_fn,
        installed_version=manager_version,
    )
    manager = ManagerUpdateService(
        paths=paths,
        config=config,
        verifier=SignatureVerifier(
            runner,
            keyring=config.release_keyring,
            fingerprints=config.release_fingerprints,
        ),
        probe=probe,
        operations=operations,
        runner=runner,
        state_mountpoint=persistent_state.record_mountpoint(paths),
        time_fn=time_fn,
        operation_log=operation_log,
        installed_version=manager_version,
        architecture=host_architecture(),
    )
    status.manager = manager
    support = SupportArchiveService(
        paths=paths, config=config, status_service=status, operations=operations, time_fn=time_fn
    )

    return ApplianceServices(
        paths=paths,
        config=config,
        runner=runner,
        probe=probe,
        docker=docker,
        systemd=systemd,
        operations=operations,
        known_good=known_good,
        admin=admin,
        packages=packages,
        network=network,
        ssh=ssh,
        backup=backup,
        support=support,
        timezone=timezone,
        auth=auth,
        status=status,
        audit=audit,
        operation_log=operation_log,
        manager=manager,
    )
