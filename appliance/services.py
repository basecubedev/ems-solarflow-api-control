# SPDX-License-Identifier: AGPL-3.0-or-later
"""Construct the appliance service graph once, for the agent and the CLI.

Everything privileged is built here, so the unprivileged web process never
imports a service that could touch Docker, apt, systemd or ``authorized_keys``.
"""

from dataclasses import dataclass

from appliance.ab_bootstrap import (
    ROLE_ADMIN,
    ROLE_EMS,
    ROLE_INFLUXDB,
    DeploymentLayout,
    RuntimeRecordStore,
    SlotBootstrapService,
    host_architecture,
)
from appliance.ab_docker_health import DockerTrialHealth
from appliance.ab_inspect import InactiveSlotInspector
from appliance.ab_layout import LayoutProbe
from appliance.ab_state import AbStateStore
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
from appliance.timezone_config import TimezoneService
from appliance.operations import OperationStore
from appliance.os_fetch import OsFetchService
from appliance.os_releases import OsReleaseCatalogue, ReleaseSource
from appliance.os_update import OsUpdateService
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
    status: object
    audit: object
    operation_log: object
    os_update: object = None
    os_fetch: object = None
    ab_probe: object = None
    ab_state: object = None
    ab_bootstrap: object = None
    ab_runtime: object = None
    ab_docker_health: object = None


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
):
    paths = paths or resolve_paths()
    if create_directories:
        ensure_directories(paths, role="agent")
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
    ab_probe = LayoutProbe(root=root, runner=runner)
    network = NetworkService(
        runner=runner,
        probe=probe,
        ab_probe=ab_probe,
        config=config,
        operations=operations,
        time_fn=time_fn,
        sleep=sleep,
        operation_log=operation_log,
        revert_intent_dir=paths.recovery_dir,
    )
    timezone = TimezoneService(paths=paths, config=config, operations=operations)
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
    # A/B is built on every appliance. On a single-slot host the layout probe
    # simply reports single_slot, and every mutating plan is blocked with a
    # reason rather than the feature being absent and unexplained.
    from appliance.version import APPLIANCE_VERSION

    ab_state = AbStateStore(paths.os_update_dir, time_fn=time_fn)
    ab_runtime = RuntimeRecordStore(paths.os_update_dir, time_fn=time_fn)
    # Container and compose-service names come from the host configuration, so
    # the deployment the appliance records is the deployment it manages.
    ab_deployment = DeploymentLayout(
        compose_file=str(deployment_compose),
        install_root=str(paths.install_root),
        containers={
            ROLE_ADMIN: config.admin_container,
            ROLE_EMS: config.ems_container,
            ROLE_INFLUXDB: config.influx_container,
        },
        services={
            ROLE_ADMIN: config.admin_service,
            ROLE_EMS: config.ems_service,
            ROLE_INFLUXDB: config.influx_service,
        },
    )
    ab_bootstrap = SlotBootstrapService(
        docker=docker,
        store=ab_runtime,
        known_good=known_good,
        deployment=ab_deployment,
        # A Pi slot must never commit with images built for another machine.
        required_platform={"os": "linux", "architecture": host_architecture()},
    )
    # One Docker contract for the trial gates, over the same backend the rest
    # of the appliance uses. Nothing here invents a second method set.
    ab_docker_health = DockerTrialHealth(
        docker,
        admin_url=config.admin_health_url,
        admin_container=config.admin_container,
        ems_container=config.ems_container,
        influx_container=config.influx_container,
    )
    os_update = OsUpdateService(
        paths=paths,
        config=config,
        operations=operations,
        catalogue=OsReleaseCatalogue(
            ReleaseSource(
                directory=config.os_release_dir,
                keyring=config.os_release_keyring,
                allow_unsigned=config.allow_unsigned_os_artifacts,
            ),
            runner=runner,
        ),
        state=ab_state,
        probe=ab_probe,
        packages=packages,
        runner=runner,
        time_fn=time_fn,
        appliance_version=APPLIANCE_VERSION,
        inspector=InactiveSlotInspector(runner=runner, root=root),
        bootstrap=ab_bootstrap,
    )
    status.os_update = os_update
    # The transport is a separate service from the one that applies an update:
    # acquiring a release and writing a slot are different authorities, and the
    # fetch needs the host probe for the clock the A/B layout probe knows
    # nothing about.
    os_fetch = OsFetchService(
        paths=paths,
        config=config,
        catalogue=os_update.catalogue,
        probe=probe,
        operations=operations,
        time_fn=time_fn,
        operation_log=operation_log,
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
        timezone=timezone,
        status=status,
        audit=audit,
        operation_log=operation_log,
        os_update=os_update,
        os_fetch=os_fetch,
        ab_probe=ab_probe,
        ab_state=ab_state,
        ab_bootstrap=ab_bootstrap,
        ab_runtime=ab_runtime,
        ab_docker_health=ab_docker_health,
    )
