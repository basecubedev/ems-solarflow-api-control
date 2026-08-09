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
from appliance.hostprobe import HostProbe
from appliance.known_good import KnownGoodStore
from appliance.network import NetworkService
from appliance.operations import OperationStore
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
    status: object
    audit: object
    operation_log: object


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
):
    paths = paths or resolve_paths()
    if create_directories:
        ensure_directories(paths)
    config = config or load_config(paths)
    runner = runner or CommandRunner()
    probe = HostProbe(runner, root=root, time_fn=time_fn)

    deployment_compose = paths.install_root / "docker-compose.admin.yml"
    if not deployment_compose.is_file():
        deployment_compose = paths.compose_file
    docker = DockerBackend(runner, compose_file=deployment_compose)

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
        catalogue=catalogue or ReleaseCatalogue(config),
        time_fn=time_fn,
        sleep=sleep,
        operation_log=operation_log,
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
    )
    ssh = SshService(
        runner=runner,
        systemd=systemd,
        config=config,
        operations=operations,
        time_fn=time_fn,
        operation_log=operation_log,
    )
    backup = BackupAccessService(paths=paths, config=config, ssh_service=ssh, probe=probe)
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
    )
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
        status=status,
        audit=audit,
        operation_log=operation_log,
    )
