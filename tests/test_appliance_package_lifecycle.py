# SPDX-License-Identifier: AGPL-3.0-or-later
"""What "installed" is allowed to mean.

``dpkg`` reporting success while the agent never started, the state migration
lost data or the web account cannot reach the socket produces an appliance that
looks installed and is not. These tests install the real package into real
guests and check the outcome the maintainer script actually reports.

Two contexts are covered on purpose: a live systemd host, where a service that
does not start is a failure, and a guest without a running systemd — an
image-build chroot — where starting services is impossible and deferring them
is correct.

Marked ``docker`` because both guests are containers.
"""

import json

import pytest

from tests.helpers.appliance_systemd import (
    AGENT_UNIT,
    LOG_DIR,
    SOCKET_PATH,
    STATE_DIR,
    WEB_UNIT,
    WEB_USER,
    OfflineRootContainer,
    SystemdContainer,
    SystemdUnavailable,
    build_package,
    docker_available,
    repack_with_version,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.skipif(not docker_available(), reason="a Docker daemon is required"), pytest.mark.appliance,]

DROPIN_DIR = "/etc/systemd/system"


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    return build_package(tmp_path_factory.mktemp("appliance-deb"))


@pytest.fixture(scope="module")
def host(package):
    container = SystemdContainer()
    try:
        container.start()
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.install_package(package)
        yield container
    finally:
        container.stop()


def break_unit(host, unit, command="/bin/false"):
    host.shell(
        f"mkdir -p {DROPIN_DIR}/{unit}.d && "
        f"printf '[Service]\\nExecStart=\\nExecStart={command}\\nRestart=no\\n' "
        f"> {DROPIN_DIR}/{unit}.d/break.conf && systemctl daemon-reload",
        timeout=120,
    )


def repair_unit(host, unit):
    host.shell(
        f"rm -rf {DROPIN_DIR}/{unit}.d && systemctl daemon-reload && "
        f"systemctl restart {unit}",
        timeout=180,
    )


def reconfigure(host):
    """Re-run the maintainer script exactly as dpkg does."""

    return host.shell(
        "DEBIAN_FRONTEND=noninteractive dpkg-reconfigure ems-appliance-manager 2>&1",
        timeout=900,
    )


# --- a clean live install ---------------------------------------------------


def test_a_clean_live_install_reports_a_usable_appliance(host):
    result = host.shell("/usr/bin/ems-appliance verify-install --json", timeout=300)
    assert result.returncode == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["ok"] is True, report["failures"]
    assert report["live_system"] is True
    statuses = {item["check"]: item["status"] for item in report["checks"]}
    assert statuses[AGENT_UNIT] == "ok", statuses
    assert statuses[WEB_UNIT] == "ok", statuses
    assert statuses["agent_socket"] == "ok", statuses
    assert statuses["web_to_agent"] == "ok", statuses
    assert statuses["state_ownership"] == "ok", statuses


def test_a_missing_optional_feature_is_reported_not_fatal(host):
    """Docker is not installed in this guest; the package still succeeds."""

    report = json.loads(host.shell("/usr/bin/ems-appliance verify-install --json").stdout)
    docker = [item for item in report["checks"] if item["check"] == "docker"][0]
    assert docker["critical"] is False
    assert docker["status"] in ("ok", "unavailable")
    if docker["status"] == "unavailable":
        assert "docker" in report["unavailable"]
    assert report["ok"] is True


def test_reinstalling_keeps_reporting_a_usable_appliance(host, package):
    host.install_package(package)
    assert host.wait_for_unit(AGENT_UNIT), host.journal(AGENT_UNIT)
    assert host.shell("/usr/bin/ems-appliance verify-install").returncode == 0


def test_an_upgrade_keeps_reporting_a_usable_appliance(host, package, tmp_path_factory):
    upgraded = repack_with_version(package, "0.1.2", tmp_path_factory.mktemp("upgrade"))
    host.install_package(upgraded)
    assert host.shell("dpkg-query -W -f='${Version}' ems-appliance-manager").stdout == "0.1.2"
    assert host.shell("/usr/bin/ems-appliance verify-install").returncode == 0


# --- critical failures are not swallowed ------------------------------------


@pytest.mark.parametrize("unit", [AGENT_UNIT, WEB_UNIT])
def test_a_failing_service_startup_fails_the_package_configuration(host, unit):
    """`systemctl restart` of a Type=simple unit returns before the process dies.

    So a broken service is not always caught by the restart itself; the
    post-install verification is what makes the package fail either way.
    """

    break_unit(host, unit)
    try:
        result = reconfigure(host)
        assert result.returncode != 0, result.stdout
        assert unit in result.stdout, result.stdout
        assert "not usable" in result.stdout or "failed to start" in result.stdout, result.stdout
    finally:
        repair_unit(host, unit)
    assert host.wait_for_unit(unit), host.journal(unit)
    assert host.shell("/usr/bin/ems-appliance verify-install").returncode == 0


def test_a_web_service_that_cannot_reach_the_agent_fails_verification(host):
    """A started pair is not enough; the socket has to be usable."""

    host.shell(f"chmod 0600 {SOCKET_PATH}", timeout=60)
    try:
        result = host.shell("/usr/bin/ems-appliance verify-install --json", timeout=300)
        report = json.loads(result.stdout)
        assert result.returncode != 0
        assert report["ok"] is False
        assert any("web_to_agent" in failure for failure in report["failures"]), report
    finally:
        host.shell(f"chmod 0660 {SOCKET_PATH}", timeout=60)
    assert host.shell("/usr/bin/ems-appliance verify-install").returncode == 0


def test_broken_state_ownership_fails_verification(host):
    host.shell(f"chown -R {WEB_USER} {STATE_DIR}/agent", timeout=120)
    try:
        result = host.shell("/usr/bin/ems-appliance verify-install --json", timeout=300)
        assert result.returncode != 0
        report = json.loads(result.stdout)
        assert any("state_ownership" in failure for failure in report["failures"]), report
    finally:
        host.shell(f"chown -R root:root {STATE_DIR}/agent", timeout=120)


# --- migration ---------------------------------------------------------------


def test_a_migration_conflict_is_reported_without_failing_the_install(host, package):
    """Both copies are preserved and the operator decides; that is not fatal."""

    host.shell(
        f"printf '{{\"generation\": \"legacy\"}}' > {STATE_DIR}/auth.json && "
        f"printf '{{\"generation\": \"current\"}}' > {STATE_DIR}/web/auth/auth.json",
        timeout=120,
    )
    result = host.install_package(package)

    assert result.returncode == 0, result.stdout
    preserved = host.shell(f"ls {STATE_DIR}/web/auth", timeout=60).stdout
    assert "auth.json.migrated-conflict" in preserved, preserved
    assert "current" in host.read_file(f"{STATE_DIR}/web/auth/auth.json")
    assert host.shell("/usr/bin/ems-appliance verify-install").returncode == 0


def test_a_fatal_migration_error_fails_the_package_configuration(host):
    """A symlinked legacy path is refused, so its state never arrives."""

    host.shell(
        f"rm -f {STATE_DIR}/auth.json; ln -sf /etc/passwd {STATE_DIR}/auth.json",
        timeout=120,
    )
    try:
        result = reconfigure(host)
        assert result.returncode != 0, result.stdout
        assert "state migration failed" in result.stdout, result.stdout
    finally:
        host.shell(f"rm -f {STATE_DIR}/auth.json", timeout=60)
    assert reconfigure(host).returncode == 0


# --- an image-build root ------------------------------------------------------


@pytest.fixture(scope="module")
def offline(package):
    container = OfflineRootContainer()
    try:
        container.start()
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        yield container
    finally:
        container.stop()


def test_an_offline_install_succeeds_without_starting_anything(offline, package):
    result = offline.install_package(package, expect_success=False)

    assert result.returncode == 0, result.stdout
    assert "start on first boot" in result.stdout, result.stdout
    assert not offline.exists("/run/systemd/system")
    assert not offline.exists(SOCKET_PATH)


def test_an_offline_install_still_builds_the_complete_layout(offline):
    for directory in (
        f"{STATE_DIR}/web/auth",
        f"{STATE_DIR}/agent/operations",
        f"{STATE_DIR}/agent/known-good",
        f"{LOG_DIR}/agent",
        f"{LOG_DIR}/audit",
    ):
        entry = offline.stat(directory)
        assert entry is not None, directory
    assert offline.stat(f"{STATE_DIR}/agent")["owner"] == "root"
    assert offline.stat(f"{STATE_DIR}/agent")["mode"] == "700"
    assert offline.stat(f"{STATE_DIR}/web")["owner"] == WEB_USER


def test_an_offline_install_enables_the_units_for_the_first_boot(offline):
    for unit in (AGENT_UNIT, WEB_UNIT, "ems-appliance-export.service"):
        assert offline.exists(
            f"/etc/systemd/system/multi-user.target.wants/{unit}"
        ), f"{unit} is not enabled for the first boot"


def test_offline_verification_defers_instead_of_failing(offline):
    result = offline.shell("/usr/bin/ems-appliance verify-install --offline --json", timeout=300)

    assert result.returncode == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["ok"] is True, report["failures"]
    assert report["live_system"] is False
    statuses = {item["check"]: item["status"] for item in report["checks"]}
    assert statuses[AGENT_UNIT] == "deferred", statuses
    assert statuses["agent_socket"] == "deferred", statuses
    assert statuses["web_to_agent"] == "deferred", statuses
    assert statuses["directories"] == "ok", statuses
    assert statuses["state_ownership"] == "ok", statuses


def test_an_offline_install_does_not_require_docker_or_networkmanager(offline):
    report = json.loads(
        offline.shell("/usr/bin/ems-appliance verify-install --offline --json").stdout
    )
    assert report["ok"] is True
    for feature in report["unavailable"]:
        entry = [item for item in report["checks"] if item["check"] == feature][0]
        assert entry["critical"] is False, entry


def test_an_offline_reinstall_stays_idempotent(offline, package):
    result = offline.install_package(package, expect_success=False)
    assert result.returncode == 0, result.stdout
    assert offline.shell("/usr/bin/ems-appliance verify-install --offline").returncode == 0


# --- the package-owned backup account ---------------------------------------


RECORD_FILE = "/var/lib/ems-appliance-manager/agent/package-state/backup-account.json"
BACKUP_HOME = "/var/lib/ems-backup"
HOME_MARKER = f"{BACKUP_HOME}/.ems-appliance-backup-home"


def record_field(host, name):
    return host.shell(
        f"python3 -c \"import json;print(json.load(open('{RECORD_FILE}')).get('{name}',''))\"",
        timeout=120,
    ).stdout.strip()


def test_the_install_records_that_it_created_the_backup_account(host):
    record = host.read_file(RECORD_FILE)

    assert '"created_by_package": true' in record, record
    assert '"account": "ems-backup"' in record, record


def test_the_install_leaves_an_ownership_marker_the_account_cannot_replace(host):
    """Device and inode are reusable; a root-owned marker in a root-owned home is not."""

    marker = host.shell(f"stat -c '%U:%G %a' {HOME_MARKER}", timeout=120).stdout.strip()
    home = host.shell(f"stat -c '%U:%G %a' {BACKUP_HOME}", timeout=120).stdout.strip()

    assert marker.startswith("root:root"), marker
    assert marker.endswith("400"), marker
    assert home.startswith("root:root"), home
    assert record_field(host, "home_marker") == HOME_MARKER
    assert len(record_field(host, "home_marker_nonce")) >= 32


def test_the_backup_account_cannot_remove_its_own_ownership_marker(host):
    result = host.shell(
        f"runuser -u ems-backup -- rm -f {HOME_MARKER} 2>&1 || true", timeout=120
    )

    assert host.shell(f"test -f {HOME_MARKER}").returncode == 0, result.stdout


def test_a_reinstall_keeps_the_ownership_marker_it_already_bound(host):
    before = record_field(host, "home_marker_nonce")

    reconfigure(host)

    assert record_field(host, "home_marker_nonce") == before
    assert host.shell(f"test -f {HOME_MARKER}").returncode == 0


def downgrade_record_to_schema_two(host):
    host.shell(
        "python3 - <<'PY'\n"
        "import json\n"
        f"path = '{RECORD_FILE}'\n"
        "record = json.load(open(path))\n"
        "record['schema_version'] = 2\n"
        "record.pop('home_marker', None)\n"
        "record.pop('home_marker_nonce', None)\n"
        "json.dump(record, open(path, 'w'))\n"
        "PY\n"
        f"rm -f {HOME_MARKER}",
        timeout=180,
    )


def test_a_legacy_record_is_not_migrated_by_a_reinstall(host):
    """A schema-2 installation is reported, never upgraded behind the operator."""

    downgrade_record_to_schema_two(host)

    result = reconfigure(host)

    assert result.returncode != 0, result.stdout
    assert host.shell(f"test -e {HOME_MARKER}").returncode != 0, "an install adopted the home"
    assert record_field(host, "schema_version") == "2"
    assert "migrate-ownership" in result.stdout, result.stdout


def test_the_explicit_migration_upgrades_a_legacy_record_in_a_real_guest(host):
    """The one command that may adopt it, on a real Debian install."""

    downgrade_record_to_schema_two(host)

    result = host.shell("ems-appliance backup-account migrate-ownership", timeout=180)

    assert result.returncode == 0, result.stdout
    assert record_field(host, "schema_version") == "3"
    assert record_field(host, "home_marker_nonce"), result.stdout
    assert host.shell(f"test -f {HOME_MARKER}").returncode == 0
    assert (
        host.shell("ems-appliance backup-account status --json", timeout=120).stdout.find(
            '"state": "current"'
        )
        >= 0
    )


def test_a_legacy_record_whose_home_was_replaced_is_not_adopted(host):
    """An uncertain legacy home stays unowned; nothing in it is touched."""

    host.shell(
        "python3 - <<'PY'\n"
        "import json\n"
        f"path = '{RECORD_FILE}'\n"
        "record = json.load(open(path))\n"
        "record['schema_version'] = 2\n"
        "record.pop('home_marker', None)\n"
        "record.pop('home_marker_nonce', None)\n"
        "json.dump(record, open(path, 'w'))\n"
        "PY\n"
        f"rm -f {HOME_MARKER} && mv {BACKUP_HOME} {BACKUP_HOME}-moved && "
        f"mkdir -p {BACKUP_HOME}/.ssh && "
        f"echo 'ssh-ed25519 AAAAoperator operator@laptop' > {BACKUP_HOME}/.ssh/authorized_keys",
        timeout=180,
    )
    try:
        result = reconfigure(host)

        assert result.returncode != 0, result.stdout
        assert host.shell(f"test -e {HOME_MARKER}").returncode != 0, "an unproven home was adopted"
        assert (
            host.read_file(f"{BACKUP_HOME}/.ssh/authorized_keys").strip()
            == "ssh-ed25519 AAAAoperator operator@laptop"
        )

        # Not even on request: the explicit adoption proves the home separately.
        requested = host.shell("ems-appliance backup-account migrate-ownership", timeout=180)

        assert requested.returncode != 0, requested.stdout
        assert host.shell(f"test -e {HOME_MARKER}").returncode != 0, "an unproven home was adopted"
        assert (
            host.read_file(f"{BACKUP_HOME}/.ssh/authorized_keys").strip()
            == "ssh-ed25519 AAAAoperator operator@laptop"
        )
    finally:
        host.shell(
            f"rm -rf {BACKUP_HOME} && mv {BACKUP_HOME}-moved {BACKUP_HOME}", timeout=120
        )
        reconfigure(host)


def test_a_replacement_home_is_never_reported_as_package_owned(host):
    """The exact case an inode the filesystem handed back would hide."""

    host.shell(
        f"mv {BACKUP_HOME} {BACKUP_HOME}-moved && mkdir -p {BACKUP_HOME}/.ssh && "
        f"python3 - <<'PY'\n"
        "import json, os\n"
        f"path = '{RECORD_FILE}'\n"
        "record = json.load(open(path))\n"
        f"entry = os.stat('{BACKUP_HOME}')\n"
        "record['home_device'] = str(entry.st_dev)\n"
        "record['home_inode'] = str(entry.st_ino)\n"
        "json.dump(record, open(path, 'w'))\n"
        "PY",
        timeout=180,
    )
    try:
        result = host.shell("/usr/bin/ems-appliance verify-install --json", timeout=300)
        report = json.loads(result.stdout)

        account = next(
            item for item in report["checks"] if item["check"] == "backup_account"
        )
        assert account["status"] != "ok", account
        assert "marker" in account["detail"], account
    finally:
        host.shell(
            f"rm -rf {BACKUP_HOME} && mv {BACKUP_HOME}-moved {BACKUP_HOME}", timeout=120
        )
        reconfigure(host)


def test_a_pre_existing_backup_account_fails_the_installation(package):
    """The package must not adopt an account an operator put on the host."""

    container = SystemdContainer()
    try:
        container.start()
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.shell(
            "useradd --create-home --home-dir /var/lib/ems-backup --shell /bin/bash ems-backup "
            "&& echo operator-file > /var/lib/ems-backup/keep-me.txt",
            timeout=120,
        )
        result = container.install_package(package, expect_success=False)

        assert result.returncode != 0, result.stdout + result.stderr
        assert "ems-backup" in (result.stdout + result.stderr)
        assert container.shell("cat /var/lib/ems-backup/keep-me.txt").stdout.strip() == (
            "operator-file"
        )
        assert container.shell("getent passwd ems-backup | cut -d: -f7").stdout.strip() == "/bin/bash"
    finally:
        container.stop()


# --- generated host configuration -------------------------------------------


def test_a_missing_generated_artefact_fails_verification(host):
    policy = "/etc/ssh/sshd_config.d/ems-appliance-backup.conf"
    saved = host.read_file(policy)
    host.shell(f"rm -f {policy}")
    try:
        result = host.shell("/usr/bin/ems-appliance verify-install", timeout=300)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "host_paths" in (result.stdout + result.stderr)
    finally:
        host.write_file(policy, saved)
        host.shell("/usr/bin/ems-appliance host-config --apply >/dev/null", timeout=300)


def test_the_generated_ssh_policy_names_the_configured_export_root(host):
    policy = host.read_file("/etc/ssh/sshd_config.d/ems-appliance-backup.conf")

    assert "ChrootDirectory /srv/ems-appliance-export" in policy, policy
    assert "Match User ems-backup" in policy, policy


def test_a_custom_install_root_reaches_the_path_watcher(package):
    container = SystemdContainer()
    try:
        container.start()
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.install_package(package)
        container.shell("mkdir -p /srv/ems-custom/config /srv/ems-custom/data /srv/ems-custom/backups")
        container.set_appliance_option("install_root", "/srv/ems-custom")
        applied = container.shell("/usr/bin/ems-appliance host-config --apply", timeout=300)
        assert applied.returncode == 0, applied.stdout + applied.stderr

        dropin = container.read_file(
            "/etc/systemd/system/ems-appliance-export.path.d/host-paths.conf"
        )
        assert "PathChanged=/srv/ems-custom" in dropin, dropin
        watched = container.unit_property("ems-appliance-export.path", "Paths")
        assert "/srv/ems-custom" in watched, watched
    finally:
        container.stop()


def test_a_custom_export_root_reaches_the_effective_ssh_policy(package):
    container = SystemdContainer()
    try:
        container.start()
    except SystemdUnavailable as exc:
        pytest.skip(str(exc))
    try:
        container.install_package(package)
        container.shell("mkdir -p /srv/ems-custom-export")
        container.set_appliance_option("export_root", "/srv/ems-custom-export")
        applied = container.shell("/usr/bin/ems-appliance host-config --apply", timeout=300)
        assert applied.returncode == 0, applied.stdout + applied.stderr

        policy = container.read_file("/etc/ssh/sshd_config.d/ems-appliance-backup.conf")
        assert "ChrootDirectory /srv/ems-custom-export" in policy, policy
        effective = container.shell(
            "sshd -T -C user=ems-backup,host=localhost,addr=127.0.0.1 | grep -i chrootdirectory",
            timeout=120,
        )
        assert "/srv/ems-custom-export" in effective.stdout, effective.stdout
    finally:
        container.stop()


def test_unmanaged_content_in_the_export_root_stops_the_export(host):
    note = "/srv/ems-appliance-export/host-note.txt"
    host.write_file(note, "operator note\n")
    try:
        result = host.shell(
            "/usr/lib/ems-appliance-manager/setup-export-root.sh 2>&1", timeout=300
        )
        assert result.returncode != 0, result.stdout
        assert "host-note.txt" in result.stdout, result.stdout
        assert host.exists(note), "operator content must never be deleted"
    finally:
        host.shell(f"rm -f {note}")
        host.setup_export_root()
