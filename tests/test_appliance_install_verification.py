# SPDX-License-Identifier: AGPL-3.0-or-later
"""What ``verify-install`` is allowed to call a usable installation.

The backup export is optional in one direction only: OpenSSH may be missing and
the EMS directories may not exist yet. A watcher that failed to start, an export
path the setup refused or a bind that is mounted read-write are not "optional" —
they are a feature that reports success while it is broken, so they have to
appear in the installation verdict.
"""

import json

from pathlib import Path

import pytest

from appliance.install_check import (
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    check_export,
)
from tests.helpers.appliance import appliance_paths

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.backup_restore]

EXPORT_SERVICE = "ems-appliance-export.service"
EXPORT_PATH = "ems-appliance-export.path"


class StubRunner:
    """Answers ``systemctl is-active``/``is-enabled`` from a scripted table."""

    def __init__(self, units=None, *, tools=("systemctl",)):
        self.units = dict(units or {})
        self.tools = set(tools)
        self.calls = []

    def available(self, tool):
        return tool in self.tools

    def run(self, tool, args=(), *, timeout=None, input_text=None, check=False):
        from appliance.commands import CommandResult

        args = list(args)
        self.calls.append((tool, tuple(args)))
        state = self.units.get(args[-1], {})
        value = state.get(args[0], "unknown") if args else "unknown"
        return CommandResult(
            tool=tool,
            args=tuple(args),
            returncode=0 if value in ("active", "enabled", "static") else 1,
            stdout=value + "\n",
            stderr="",
        )


def healthy_units():
    return {
        EXPORT_SERVICE: {"is-active": "inactive", "is-enabled": "enabled"},
        EXPORT_PATH: {"is-active": "active", "is-enabled": "enabled"},
    }


def write_status(paths, payload):
    paths.export_status_file.parent.mkdir(parents=True, exist_ok=True)
    paths.export_status_file.write_text(json.dumps(payload), encoding="utf-8")
    return paths.export_status_file


def configured_status(paths):
    return {
        "status": "configured",
        "user": "ems-backup",
        "root": str(paths.install_root),
        "export_root": str(paths.export_root),
        "chroot": True,
        "detail": "",
        "paths": [
            {
                "name": name,
                "source": str(paths.install_root / name),
                "target": str(target),
                "state": "mounted",
                "read_only": True,
            }
            for name, target in paths.export_targets().items()
        ],
    }


def mounts(paths, *, read_only=True, names=None, source_for=None):
    """The mount records ``/proc/1/mountinfo`` would produce for the binds.

    ``root`` is the path of the mounted subtree inside its own filesystem and
    ``device`` is that filesystem's device number, so the pair is the kernel's
    own statement about which directory is published at a mount point.
    """

    options = frozenset({"ro", "relatime"} if read_only else {"rw", "relatime"})
    table = {}
    for name, target in paths.export_targets().items():
        if names is not None and name not in names:
            continue
        source = (source_for or (lambda item: str(paths.install_root / item)))(name)
        table[str(target)] = {
            "options": options,
            "root": source,
            "device": device_of(paths.install_root),
            "source": "/dev/root",
            "fstype": "ext4",
        }
    return table


def device_of(path):
    import os

    entry = os.stat(str(path))
    return f"{os.major(entry.st_dev)}:{os.minor(entry.st_dev)}"


def seed_sources(paths, *, names=("config", "backups", "data")):
    for name in names:
        (paths.install_root / name).mkdir(parents=True, exist_ok=True)
    return paths.install_root


def by_name(checks):
    return {item["check"]: item for item in checks}


def critical_failures(checks):
    return [item for item in checks if item["critical"] and item["status"] == STATUS_FAILED]


# --- the healthy case -------------------------------------------------------


def test_a_configured_export_is_verified_against_the_mount_table(tmp_path, production_chroot_chain):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    write_status(paths, configured_status(paths))

    checks = check_export(
        paths,
        StubRunner(healthy_units()),
        live=True,
        mounts=mounts(paths),
        sshd=True,
    )

    named = by_name(checks)
    assert named["export_service"]["status"] == STATUS_OK, named
    assert named["export_path_unit"]["status"] == STATUS_OK, named
    assert named["export_setup"]["status"] == STATUS_OK, named
    assert not critical_failures(checks)


# --- optional, not broken ---------------------------------------------------


def test_a_missing_openssh_makes_the_feature_unavailable_without_failing(tmp_path):
    paths = appliance_paths(tmp_path)
    write_status(paths, {"status": "unavailable", "detail": "sshd is not installed", "paths": []})

    checks = check_export(paths, StubRunner(healthy_units()), live=True, mounts={}, sshd=False)

    assert by_name(checks)["export_setup"]["status"] == STATUS_UNAVAILABLE
    assert not critical_failures(checks)


def test_missing_ems_directories_are_pending_and_do_not_fail_the_package(tmp_path):
    paths = appliance_paths(tmp_path)
    write_status(
        paths,
        {"status": "pending", "detail": "no EMS installation found yet", "paths": []},
    )

    checks = check_export(paths, StubRunner(healthy_units()), live=True, mounts={}, sshd=True)

    assert by_name(checks)["export_setup"]["status"] == STATUS_OK
    assert "pending" in by_name(checks)["export_setup"]["detail"]
    assert not critical_failures(checks)


def test_an_offline_build_root_defers_the_export_checks(tmp_path):
    paths = appliance_paths(tmp_path)

    checks = check_export(paths, StubRunner({}), live=False, mounts={}, sshd=True)

    assert all(item["status"] == STATUS_DEFERRED for item in checks), checks
    assert not critical_failures(checks)


# --- broken, and visible ----------------------------------------------------


def test_a_path_watcher_that_did_not_start_fails_the_installation(tmp_path):
    paths = appliance_paths(tmp_path)
    write_status(paths, configured_status(paths))
    seed_sources(paths)
    units = healthy_units()
    units[EXPORT_PATH]["is-active"] = "failed"

    checks = check_export(
        paths,
        StubRunner(units),
        live=True,
        mounts=mounts(paths),
        sshd=True,
    )

    assert by_name(checks)["export_path_unit"]["status"] == STATUS_FAILED
    assert critical_failures(checks)


def test_a_refused_export_path_fails_the_installation(tmp_path):
    paths = appliance_paths(tmp_path)
    write_status(
        paths,
        {
            "status": "failed",
            "detail": "config is not a directory inside the EMS installation",
            "paths": [],
        },
    )

    checks = check_export(paths, StubRunner(healthy_units()), live=True, mounts={}, sshd=True)

    assert by_name(checks)["export_setup"]["status"] == STATUS_FAILED
    assert critical_failures(checks)


def test_a_read_write_bind_is_reported_instead_of_being_ignored(tmp_path):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    write_status(paths, configured_status(paths))

    checks = check_export(
        paths,
        StubRunner(healthy_units()),
        live=True,
        mounts=mounts(paths, read_only=False),
        sshd=True,
    )

    assert by_name(checks)["export_setup"]["status"] == STATUS_FAILED
    assert critical_failures(checks)


def test_a_recorded_mount_the_kernel_does_not_have_is_not_believed(tmp_path):
    """The status file is a report, the mount table is the evidence."""

    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    write_status(paths, configured_status(paths))

    checks = check_export(paths, StubRunner(healthy_units()), live=True, mounts={}, sshd=True)

    assert by_name(checks)["export_setup"]["status"] == STATUS_FAILED
    assert "mount" in by_name(checks)["export_setup"]["detail"].lower()


def test_a_disabled_export_service_is_a_failure(tmp_path):
    paths = appliance_paths(tmp_path)
    write_status(paths, configured_status(paths))
    seed_sources(paths)
    units = healthy_units()
    units[EXPORT_SERVICE]["is-enabled"] = "disabled"

    checks = check_export(
        paths,
        StubRunner(units),
        live=True,
        mounts=mounts(paths),
        sshd=True,
    )

    assert by_name(checks)["export_service"]["status"] == STATUS_FAILED


# --- the status file is diagnostic input, never the authority ---------------


def verify(paths, *, mounts_table=None, units=None):
    return by_name(
        check_export(
            paths,
            StubRunner(units or healthy_units()),
            live=True,
            mounts=mounts_table if mounts_table is not None else mounts(paths),
            sshd=True,
        )
    )


def test_a_configured_report_with_no_paths_is_not_believed(tmp_path):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    write_status(paths, {"status": "configured", "detail": "", "paths": []})

    named = verify(paths)

    assert named["export_setup"]["status"] == STATUS_FAILED, named


def test_a_configured_report_missing_an_expected_export_is_not_believed(tmp_path):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    payload = configured_status(paths)
    payload["paths"] = [item for item in payload["paths"] if item["name"] != "data"]
    write_status(paths, payload)

    named = verify(paths)

    assert named["export_setup"]["status"] == STATUS_FAILED, named
    assert "data" in named["export_setup"]["detail"], named


def test_a_configured_report_with_an_extra_export_is_not_believed(tmp_path):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    payload = configured_status(paths)
    payload["paths"].append(
        {
            "name": "secrets",
            "source": str(paths.install_root / "secrets"),
            "target": str(paths.export_root / "secrets"),
            "state": "mounted",
            "read_only": True,
        }
    )
    write_status(paths, payload)

    named = verify(paths)

    assert named["export_setup"]["status"] == STATUS_FAILED, named


def test_a_configured_report_with_a_wrong_source_is_not_believed(tmp_path):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    payload = configured_status(paths)
    payload["paths"][0]["source"] = "/etc"
    write_status(paths, payload)

    named = verify(paths)

    assert named["export_setup"]["status"] == STATUS_FAILED, named


def test_a_configured_report_with_a_wrong_target_is_not_believed(tmp_path):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    payload = configured_status(paths)
    payload["paths"][0]["target"] = "/srv/somewhere-else/config"
    write_status(paths, payload)

    named = verify(paths)

    assert named["export_setup"]["status"] == STATUS_FAILED, named


def test_a_foreign_read_only_mount_at_an_expected_target_is_not_confined(tmp_path):
    """``ro`` is not evidence: the kernel must publish the configured source."""

    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    write_status(paths, configured_status(paths))
    table = mounts(paths)
    table[str(paths.export_root / "config")]["root"] = "/etc"

    named = verify(paths, mounts_table=table)

    assert named["export_setup"]["status"] == STATUS_FAILED, named


def test_an_unexpected_entry_inside_the_export_root_fails_verification(tmp_path):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    write_status(paths, configured_status(paths))
    (paths.export_root / "host-note.txt").write_text("note\n", encoding="utf-8")

    named = verify(paths)

    assert named["export_setup"]["status"] == STATUS_FAILED, named
    assert "host-note.txt" in named["export_setup"]["detail"], named


def test_a_missing_ems_directory_stays_pending_and_exact(tmp_path, production_chroot_chain):
    paths = appliance_paths(tmp_path)
    seed_sources(paths, names=("config", "backups"))
    payload = configured_status(paths)
    for entry in payload["paths"]:
        if entry["name"] == "data":
            entry["state"] = "missing"
    write_status(paths, payload)

    named = verify(paths, mounts_table=mounts(paths, names=("config", "backups")))

    assert named["export_setup"]["status"] == STATUS_OK, named


# --- the package-owned backup account ---------------------------------------


def write_account_record(paths, **fields):
    """The record an installation writes, with its home bound to that directory."""

    from appliance import backup_ownership

    home = paths.state_dir / "backup-home"
    home.mkdir(parents=True, exist_ok=True)
    entry = home.stat()
    nonce = backup_ownership.new_marker_nonce()
    uid = fields.get("uid", 1500)
    marker = home / backup_ownership.HOME_MARKER_NAME
    marker.write_text(
        backup_ownership.render_home_marker(
            account="ems-backup",
            uid=uid,
            primary_gid=fields.get("primary_gid", uid),
            home=str(home),
            installation_id="test-installation",
            nonce=nonce,
        ),
        encoding="utf-8",
    )
    marker.chmod(0o400)
    payload = {
        "schema_version": backup_ownership.RECORD_SCHEMA_VERSION,
        "account": "ems-backup",
        "created_by_package": True,
        "uid": 1500,
        "primary_gid": 1500,
        "home": str(home),
        "home_device": str(entry.st_dev),
        "home_inode": str(entry.st_ino),
        "home_marker": str(marker),
        "home_marker_nonce": nonce,
        "home_created_by_package": True,
        "installation_id": "test-installation",
    }
    payload.update(fields)
    paths.package_state_dir.mkdir(parents=True, exist_ok=True)
    (paths.package_state_dir / "backup-account.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return payload


def account_check(paths, monkeypatch, *, exists=True, shell="/usr/sbin/nologin", home=None):
    import pwd

    from appliance.config import ApplianceConfig

    home = home or str(paths.state_dir / "backup-home")

    class Entry:
        pw_dir = home
        pw_shell = shell
        pw_uid = 1500
        pw_gid = 1500

    def getpwnam(name):
        if not exists:
            raise KeyError(name)
        return Entry()

    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    from appliance.install_check import check_backup_account

    return by_name(check_backup_account(paths, ApplianceConfig()))["backup_account"]


def test_a_package_owned_account_is_reported_as_usable(tmp_path, monkeypatch):
    paths = appliance_paths(tmp_path)
    write_account_record(paths)

    assert account_check(paths, monkeypatch)["status"] == STATUS_OK


def test_an_account_without_an_ownership_record_is_a_visible_conflict(tmp_path, monkeypatch):
    paths = appliance_paths(tmp_path)

    entry = account_check(paths, monkeypatch)

    assert entry["status"] == STATUS_FAILED, entry
    assert entry["critical"] is True
    assert "ownership record" in entry["detail"]


def test_an_account_with_a_shell_is_a_failure(tmp_path, monkeypatch):
    paths = appliance_paths(tmp_path)
    write_account_record(paths)

    entry = account_check(paths, monkeypatch, shell="/bin/bash")

    assert entry["status"] == STATUS_FAILED, entry


def test_unresolved_key_conflicts_are_reported(tmp_path, monkeypatch):
    paths = appliance_paths(tmp_path)
    write_account_record(paths)
    home = paths.state_dir / "backup-home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh" / "authorized_keys.disabled-by-appliance.conflict").write_text(
        "ssh-ed25519 AAAA x\n", encoding="utf-8"
    )

    entry = account_check(paths, monkeypatch, home=str(home))

    assert entry["status"] == STATUS_FAILED, entry
    assert "conflict" in entry["detail"]


def test_a_missing_account_is_unavailable_not_broken(tmp_path, monkeypatch):
    paths = appliance_paths(tmp_path)

    entry = account_check(paths, monkeypatch, exists=False)

    assert entry["status"] == STATUS_UNAVAILABLE, entry
    assert entry["critical"] is False


def test_a_failed_export_service_fails_the_installation(tmp_path):
    paths = appliance_paths(tmp_path)
    seed_sources(paths)
    write_status(paths, configured_status(paths))
    units = healthy_units()
    units[EXPORT_SERVICE]["is-active"] = "failed"

    named = verify(paths, units=units)

    assert named["export_service"]["status"] == STATUS_FAILED, named


def test_a_replaced_account_is_not_reported_as_package_owned(tmp_path, monkeypatch):
    """Same name, different uid: removal must not believe it owns this account."""

    paths = appliance_paths(tmp_path)
    write_account_record(paths, uid=4242)

    entry = account_check(paths, monkeypatch)

    assert entry["status"] == STATUS_FAILED, entry
    assert "uid" in entry["detail"], entry


def test_a_legacy_ownership_record_is_not_accepted_as_proof(tmp_path, monkeypatch):
    paths = appliance_paths(tmp_path)
    paths.package_state_dir.mkdir(parents=True, exist_ok=True)
    (paths.package_state_dir / "backup-account.json").write_text(
        json.dumps({"account": "ems-backup", "created_by_package": True}), encoding="utf-8"
    )

    entry = account_check(paths, monkeypatch)

    assert entry["status"] == STATUS_FAILED, entry
    assert "identity binding" in entry["detail"], entry


# --- the A/B tool contract ----------------------------------------------------


def test_verify_install_reports_every_ab_tool_it_needs():
    from appliance import install_check

    names = {item["check"] for item in install_check.check_ab_tools()}

    for tool, _package, _purpose in install_check.AB_REQUIRED_TOOLS:
        assert f"ab_tool:{tool}" in names
    assert "ab_artifact_decoder" in names


def test_a_missing_tool_names_the_package_that_provides_it(monkeypatch):
    from appliance import install_check

    monkeypatch.setattr(install_check, "_which", lambda tool: "" if tool == "zstd" else "/usr/bin/x")
    checks = {item["check"]: item for item in install_check.check_ab_tools()}

    assert checks["ab_tool:zstd"]["status"] == install_check.STATUS_FAILED
    assert "install zstd" in checks["ab_tool:zstd"]["detail"]
    assert checks["ab_artifact_decoder"]["status"] == install_check.STATUS_FAILED


def test_the_sparse_decoder_needs_no_external_package():
    """It is implemented in appliance/sparse.py, so it is always ready."""

    from appliance import install_check

    assert install_check.ab_decoder_state()["sparse_decoder_ready"] is True
    assert not any(
        "sparse" in package for _tool, package, _purpose in install_check.AB_REQUIRED_TOOLS
    )


def test_the_declared_package_dependencies_cover_every_required_ab_tool():
    from appliance import install_check

    control = _control_file().read_text(encoding="utf-8")
    depends = control.split("Depends:", 1)[1].split("\nRecommends:", 1)[0]

    for _tool, package, _purpose in install_check.AB_REQUIRED_TOOLS:
        assert package in depends, package


def test_the_optional_ab_tools_are_recommended_not_required():
    from appliance import install_check

    control = _control_file().read_text(encoding="utf-8")
    recommends = control.split("Recommends:", 1)[1].split("\nHomepage:", 1)[0]

    for _tool, package, _purpose in install_check.AB_OPTIONAL_TOOLS:
        assert package in recommends, package


def _control_file():
    return Path(__file__).resolve().parents[1] / "packaging/appliance/debian/control"
