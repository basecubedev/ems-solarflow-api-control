# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first Admin installation on a freshly flashed appliance.

The image ships ``/opt/ems-solarflow`` as an empty mount point, so there is no
compose file for the normal install path to edit and nobody who would ever have
created one. These tests drive the real service the way the browser does:
planning stays a preview that writes nothing, execution creates the deployment
with the packaged installer, and a deployment that appeared in between is never
overwritten.
"""

import subprocess
from pathlib import Path

import pytest

from appliance import admin_bootstrap
from appliance.admin_bootstrap import BootstrapError, DeploymentBootstrap
from appliance.admin_deployment import resolve_deployment
from appliance.agent import AgentHandlers
from appliance.operations import STATE_FAILED_RECOVERABLE, STATE_FAILED_TERMINAL, STATE_SUCCEEDED
from tests.helpers.appliance import (
    ADMIN_CONTAINER,
    ADMIN_REPOSITORY,
    ADMIN_SERVICE,
    COMPOSE_TEMPLATE,
    EMS_CONTAINER,
    StaticCatalogue,
    build_test_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SHIPPED_INSTALLER = ROOT / "deploy" / "admin" / "install-admin-console.sh"


class RecordingBootstrap:
    """The packaged installer, replaced by what it is contracted to produce.

    It records the arguments so a test can prove the operator's version choice
    reached the installer, and writes the deployment the real script writes so
    the rest of the install path runs unchanged.
    """

    def __init__(self, paths, *, uid=1000, gid=1000, fails=False, writes=True):
        self.paths = paths
        self.uid = uid
        self.gid = gid
        self.fails = fails
        self.writes = writes
        self.calls = []
        self.identity_calls = []

    def identity(self, *, claim=False):
        self.identity_calls.append(claim)
        return (self.uid, self.gid)

    def run(self, *, tag, uid, gid):
        self.calls.append({"tag": tag, "uid": uid, "gid": gid})
        if self.fails:
            raise BootstrapError("installer_failed", "the Admin installer failed: no output")
        if self.writes:
            self.paths.install_root.mkdir(parents=True, exist_ok=True)
            (self.paths.install_root / "docker-compose.admin.yml").write_text(
                COMPOSE_TEMPLATE.format(
                    service=ADMIN_SERVICE,
                    image=f"{ADMIN_REPOSITORY}:{tag}",
                    container=ADMIN_CONTAINER,
                    ems_service="ems",
                    ems_container=EMS_CONTAINER,
                ),
                encoding="utf-8",
            )
            (self.paths.install_root / ".env.admin").write_text(
                f"EMS_INSTALL_DIR={self.paths.install_root}\n", encoding="utf-8"
            )
        return {"installer": "recording", "tag": tag, "uid": uid, "gid": gid}


def flashed_appliance(tmp_path, *, bootstrap=None, tags=("v1.1.0", "v1.0.0")):
    """An appliance whose deployment root is an empty directory."""

    services = build_test_services(
        tmp_path, catalogue=StaticCatalogue(list(tags)), admin_bootstrap=bootstrap
    )
    if bootstrap is not None:
        bootstrap.paths = services.paths
    for tag in tags:
        services.host.publish_image(tag)
    return services


def plan(services, **fields):
    handlers = AgentHandlers(services, executor=lambda target: target())
    return handlers.dispatch({"operation": "admin.plan_install", **fields})


def execute(services, planned):
    handlers = AgentHandlers(services, executor=lambda target: target())
    handlers.dispatch(
        {
            "operation": "operations.execute",
            "operation_id": planned["operation"]["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    return services.operations.get(planned["operation"]["operation_id"])


def refused(services, **fields):
    handlers = AgentHandlers(services, executor=lambda target: target())
    with pytest.raises(Exception) as excinfo:
        handlers.dispatch({"operation": "admin.plan_install", **fields})
    return getattr(excinfo.value, "code", "")


# --- detection -------------------------------------------------------------


def test_a_flashed_appliance_reports_that_admin_must_be_created_first(tmp_path):
    services = flashed_appliance(tmp_path, bootstrap=RecordingBootstrap(None))

    detected = services.admin.detect()

    assert detected["bootstrap_required"] is True
    assert detected["installed"] is False
    assert detected["deployment"]["service_defined"] is False


def test_an_existing_deployment_is_not_a_bootstrap(tmp_path):
    services = flashed_appliance(tmp_path, bootstrap=RecordingBootstrap(None))
    services.host.write_deployment(tag="v1.0.0")

    assert services.admin.detect()["bootstrap_required"] is False


def test_a_container_without_a_compose_file_is_not_a_bootstrap(tmp_path):
    """Something else created it; writing files around it is a second authority."""

    services = flashed_appliance(tmp_path, bootstrap=RecordingBootstrap(None))
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")

    assert services.admin.detect()["bootstrap_required"] is False
    assert refused(services, channel="exact", tag="v1.1.0") == "compose_file_missing"


# --- planning is a preview -------------------------------------------------


def test_planning_a_first_installation_announces_the_bootstrap(tmp_path):
    bootstrap = RecordingBootstrap(None)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)

    planned = plan(services, channel="exact", tag="v1.1.0")

    assert planned["plan"]["bootstrap"] is True
    assert planned["plan"]["creates_deployment"]["compose_file"].endswith(
        "docker-compose.admin.yml"
    )
    assert planned["plan"]["target_tag"] == "v1.1.0"


def test_planning_writes_no_deployment_file(tmp_path):
    """The UI shows a plan as a consequence-free preview, so it has to be one."""

    bootstrap = RecordingBootstrap(None)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)

    plan(services, channel="exact", tag="v1.1.0")

    assert not (services.paths.install_root / "docker-compose.admin.yml").exists()
    assert not (services.paths.install_root / ".env.admin").exists()
    assert bootstrap.calls == []
    # Asking who would own the deployment is a question, not a handover.
    assert bootstrap.identity_calls == [False]


def test_planning_refuses_when_the_deployment_root_has_no_usable_owner(tmp_path):
    class RootOwned(RecordingBootstrap):
        def identity(self, *, claim=False):
            raise BootstrapError(
                "deployment_root_root_owned", "/opt/ems-solarflow is owned by root"
            )

    services = flashed_appliance(tmp_path, bootstrap=RootOwned(None))

    assert refused(services, channel="exact", tag="v1.1.0") == "deployment_root_root_owned"


def test_a_first_installation_cannot_ask_for_a_version_it_never_had(tmp_path):
    services = flashed_appliance(tmp_path, bootstrap=RecordingBootstrap(None))

    assert refused(services, channel="previous_known_good") != ""


# --- execution -------------------------------------------------------------


def test_executing_the_plan_creates_the_deployment_and_installs_the_chosen_version(tmp_path):
    bootstrap = RecordingBootstrap(None)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)

    record = execute(services, plan(services, channel="exact", tag="v1.1.0"))

    assert record.state == STATE_SUCCEEDED
    assert record.result["bootstrapped"] is True
    assert record.result["installed_version"] == "v1.1.0"
    assert services.admin.detect()["installed"] is True
    assert services.admin.detect()["bootstrap_required"] is False


def test_the_operators_version_choice_reaches_the_installer(tmp_path):
    bootstrap = RecordingBootstrap(None)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)

    execute(services, plan(services, channel="exact", tag="v1.0.0"))

    assert [call["tag"] for call in bootstrap.calls] == ["v1.0.0"]


def test_the_created_deployment_is_pinned_to_the_validated_digest(tmp_path):
    """The installer names a tag; what actually runs is the digest the plan showed."""

    bootstrap = RecordingBootstrap(None)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)

    planned = plan(services, channel="exact", tag="v1.1.0")
    execute(services, planned)

    compose = (services.paths.install_root / "docker-compose.admin.yml").read_text(
        encoding="utf-8"
    )
    assert planned["plan"]["target_digest"] in compose


def test_the_created_deployment_is_recorded_as_known_good(tmp_path):
    bootstrap = RecordingBootstrap(None)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)

    execute(services, plan(services, channel="exact", tag="v1.1.0"))

    assert services.known_good.current()["admin_version"] == "v1.1.0"


# --- what a bootstrap may never do -----------------------------------------


def test_a_deployment_that_appeared_after_the_plan_is_never_overwritten(tmp_path):
    """The plan bound itself to there being nothing; nothing is what it may find."""

    bootstrap = RecordingBootstrap(None)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)
    planned = plan(services, channel="exact", tag="v1.1.0")

    services.host.write_deployment(tag="v1.0.0")
    existing = (services.paths.install_root / "docker-compose.admin.yml").read_text(
        encoding="utf-8"
    )
    record = execute(services, planned)

    assert record.state == STATE_FAILED_TERMINAL
    assert record.error["code"] == "deployment_appeared_since_plan"
    assert bootstrap.calls == []
    assert (services.paths.install_root / "docker-compose.admin.yml").read_text(
        encoding="utf-8"
    ) == existing


def test_a_failed_installer_leaves_the_appliance_without_admin_and_says_so(tmp_path):
    bootstrap = RecordingBootstrap(None, fails=True)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)

    record = execute(services, plan(services, channel="exact", tag="v1.1.0"))

    assert record.state == STATE_FAILED_RECOVERABLE
    assert record.error["code"] == "installer_failed"
    assert "still has no Admin installation" in record.error["message"]
    assert record.result["deployment_created"] is False
    assert services.admin.detect()["bootstrap_required"] is True


def test_an_installer_that_writes_nothing_usable_is_reported_not_assumed(tmp_path):
    bootstrap = RecordingBootstrap(None, writes=False)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)

    record = execute(services, plan(services, channel="exact", tag="v1.1.0"))

    assert record.state == STATE_FAILED_RECOVERABLE
    assert record.error["code"] == "bootstrap_incomplete"


def test_a_first_installation_that_never_becomes_healthy_is_not_rolled_back(tmp_path):
    """There is no previous Admin, so a rollback could only restart the failure."""

    bootstrap = RecordingBootstrap(None)
    services = flashed_appliance(tmp_path, bootstrap=bootstrap)
    planned = plan(services, channel="exact", tag="v1.1.0")
    services.host.compose_up_fails = True

    record = execute(services, planned)

    assert record.state == STATE_FAILED_RECOVERABLE
    assert record.result["bootstrap_failed"] is True
    assert record.result["deployment_created"] is True


def test_the_bootstrap_flag_cannot_be_added_after_confirmation(tmp_path):
    """It decides whether the appliance may write a deployment file at all."""

    services = build_test_services(tmp_path, catalogue=StaticCatalogue(["v1.1.0", "v1.0.0"]))
    services.host.write_deployment(tag="v1.0.0")
    services.host.publish_image("v1.1.0")
    services.host.publish_image("v1.0.0")
    services.host.pull_local(f"{ADMIN_REPOSITORY}:v1.0.0")
    services.host.run_container(ADMIN_CONTAINER, f"{ADMIN_REPOSITORY}:v1.0.0")
    planned = plan(services, channel="exact", tag="v1.1.0")

    operation_id = planned["operation"]["operation_id"]
    services.operations.update_target(operation_id, {"bootstrap": True})
    with pytest.raises(Exception) as excinfo:
        execute(services, planned)

    record = services.operations.get(operation_id)
    assert getattr(excinfo.value, "code", "") == "operation_plan_changed"
    assert record.state == STATE_FAILED_TERMINAL
    assert record.result["admin_untouched"] is True


# --- the packaged installer ------------------------------------------------


def test_the_installer_is_the_one_the_package_ships(monkeypatch):
    monkeypatch.delenv("EMS_APPLIANCE_LIBDIR", raising=False)

    assert str(admin_bootstrap.installer_path()) == (
        "/usr/lib/ems-appliance-manager/install-admin-console.sh"
    )


def test_a_missing_installer_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_APPLIANCE_LIBDIR", str(tmp_path / "empty"))
    services = build_test_services(tmp_path)
    bootstrap = DeploymentBootstrap(services.paths, services.config)

    with pytest.raises(BootstrapError) as excinfo:
        bootstrap.run(tag="v1.1.0", uid=1000, gid=1000)

    assert excinfo.value.code == "installer_missing"


def test_the_shipped_installer_writes_a_deployment_the_appliance_can_resolve(
    tmp_path, monkeypatch
):
    """The one test that runs the real script instead of trusting a fixture.

    Every blocker this appliance hit on real hardware came from a fixture that
    promised what the real thing does not do. The installer is a shipped shell
    script with its own idea of file names and service names, so the contract
    between it and ``resolve_deployment`` is checked against the script itself.
    """

    monkeypatch.setenv("EMS_APPLIANCE_LIBDIR", str(SHIPPED_INSTALLER.parent))
    services = build_test_services(tmp_path)
    bootstrap = DeploymentBootstrap(services.paths, services.config)

    entry = services.paths.install_root.stat()
    record = bootstrap.run(tag="v1.1.0", uid=entry.st_uid, gid=entry.st_gid)

    deployment = resolve_deployment(services.paths, services.config)
    assert record["tag"] == "v1.1.0"
    assert deployment.service_defined is True
    assert deployment.compose_exists is True
    assert deployment.image_reference == f"{ADMIN_REPOSITORY}:v1.1.0"


def test_the_shipped_installer_is_never_asked_to_overwrite_or_to_start(tmp_path, monkeypatch):
    """``--force`` would replace somebody's deployment; a start would bypass
    the digest pin and the health verification the install path owns."""

    monkeypatch.setenv("EMS_APPLIANCE_LIBDIR", str(SHIPPED_INSTALLER.parent))
    services = build_test_services(tmp_path)
    recorded = {}

    def fake_run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    bootstrap = DeploymentBootstrap(services.paths, services.config, runner=fake_run)
    bootstrap.run(tag="v1.1.0", uid=1000, gid=1000)

    assert "--no-start" in recorded["command"]
    assert "--force" not in recorded["command"]
    assert recorded["command"][:3] == [str(SHIPPED_INSTALLER), "--tag", "v1.1.0"]
    assert recorded["kwargs"]["env"]["PUID"] == "1000"
    assert recorded["kwargs"]["env"]["PGID"] == "1000"


def test_privileges_are_dropped_to_the_deployment_owner_when_there_are_any(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EMS_APPLIANCE_LIBDIR", str(SHIPPED_INSTALLER.parent))
    services = build_test_services(tmp_path)
    recorded = {}

    def fake_run(command, **kwargs):
        recorded.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    bootstrap = DeploymentBootstrap(
        services.paths, services.config, runner=fake_run, geteuid=lambda: 0
    )
    bootstrap.run(tag="v1.1.0", uid=1500, gid=1500)

    assert recorded["user"] == 1500
    assert recorded["group"] == 1500
    assert recorded["extra_groups"] == []


# --- who owns the deployment root ------------------------------------------


def test_an_existing_installation_keeps_the_owner_it_already_has(tmp_path):
    services = build_test_services(tmp_path)
    services.host.write_deployment(tag="v1.0.0")
    bootstrap = DeploymentBootstrap(services.paths, services.config)

    entry = services.paths.install_root.stat()
    assert bootstrap.identity() == (entry.st_uid, entry.st_gid)


def test_a_root_owned_deployment_root_nothing_was_installed_in_is_adopted(tmp_path):
    """A flashed appliance: the root is root's only because nobody claimed it."""

    services = build_test_services(tmp_path)
    chowned = []
    bootstrap = DeploymentBootstrap(
        services.paths,
        services.config,
        stat=lambda path: RootOwned(),
        chown=lambda path, uid, gid: chowned.append((str(path), uid, gid)),
        lookup=lambda name: account(name, 700, 700),
    )

    assert bootstrap.identity(claim=True) == (700, 700)
    assert (str(services.paths.install_root), 700, 700) in chowned


def test_asking_who_would_own_the_deployment_changes_nothing(tmp_path):
    """The plan shows the owner; only the confirmed execution hands it over."""

    services = build_test_services(tmp_path)
    chowned = []
    bootstrap = DeploymentBootstrap(
        services.paths,
        services.config,
        stat=lambda path: RootOwned(),
        chown=lambda path, uid, gid: chowned.append(path),
        lookup=lambda name: account(name, 700, 700),
    )

    assert bootstrap.identity() == (700, 700)
    assert chowned == []


def test_the_directories_a_boot_scaffolded_are_handed_over_with_the_root(tmp_path):
    """Otherwise the installer cannot write inside the deployment it just got."""

    services = build_test_services(tmp_path)
    chowned = []
    bootstrap = DeploymentBootstrap(
        services.paths,
        services.config,
        stat=lambda path: RootOwned(),
        chown=lambda path, uid, gid: chowned.append(Path(path)),
        lookup=lambda name: account(name, 700, 700),
    )

    bootstrap.identity(claim=True)

    # From the map production uses. Naming install_root/<name> here is the
    # restatement that let /backups point at a directory nothing writes to.
    for source in services.paths.export_paths().values():
        assert source in chowned, source


def test_a_root_owned_deployment_root_holding_an_installation_is_never_taken_over(tmp_path):
    services = build_test_services(tmp_path)
    services.host.write_deployment(tag="v1.0.0")
    bootstrap = DeploymentBootstrap(
        services.paths,
        services.config,
        stat=lambda path: RootOwned(),
        lookup=lambda name: account(name, 700, 700),
    )

    with pytest.raises(BootstrapError) as excinfo:
        bootstrap.identity()

    assert excinfo.value.code == "deployment_root_root_owned"


def test_a_missing_deployment_account_is_named_not_worked_around(tmp_path):
    services = build_test_services(tmp_path)

    def missing(name):
        raise KeyError(name)

    bootstrap = DeploymentBootstrap(
        services.paths, services.config, stat=lambda path: RootOwned(), lookup=missing
    )

    with pytest.raises(BootstrapError) as excinfo:
        bootstrap.identity()

    assert excinfo.value.code == "deployment_account_missing"
    assert services.config.deployment_user in excinfo.value.message


def test_a_deployment_account_that_resolves_to_root_is_refused(tmp_path):
    """The hosted containers run as this identity; root is not one of them."""

    services = build_test_services(tmp_path)
    bootstrap = DeploymentBootstrap(
        services.paths,
        services.config,
        stat=lambda path: RootOwned(),
        lookup=lambda name: account(name, 0, 0),
    )

    with pytest.raises(BootstrapError) as excinfo:
        bootstrap.identity()

    assert excinfo.value.code == "deployment_account_privileged"


class RootOwned:
    st_uid = 0
    st_gid = 0


def account(name, uid, gid):
    import pwd

    return pwd.struct_passwd((name, "x", uid, gid, "", "/nonexistent", "/usr/sbin/nologin"))
