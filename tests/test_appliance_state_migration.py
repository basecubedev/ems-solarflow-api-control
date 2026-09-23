# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migrating an existing installation into the web/agent state split.

An appliance that is already running holds the operator's password, the
known-good history and the operation records. The migration therefore copies
and verifies before it removes anything, refuses a symlinked source and keeps
both copies when the two layouts disagree.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from appliance import manager_verify
from appliance.migration import (
    RESULT_ALREADY_DONE,
    RESULT_CONFLICT,
    RESULT_MIGRATED,
    RESULT_REFUSED,
    RESULT_SKIPPED,
    migrate_state,
    write_report,
)
from tests.helpers.appliance import appliance_paths

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]


def legacy_installation(tmp_path):
    """An appliance as the previous release left it: one shared state tree."""

    paths = appliance_paths(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)

    paths.legacy_auth_file.write_text('{"algorithm": "pbkdf2-sha256", "generation": "abc"}')
    paths.legacy_state_file.write_text('{"seen": true}')

    paths.legacy_operations_dir.mkdir(parents=True, exist_ok=True)
    (paths.legacy_operations_dir / ("a" * 32 + ".json")).write_text('{"type": "admin.install"}')

    paths.legacy_known_good_dir.mkdir(parents=True, exist_ok=True)
    (paths.legacy_known_good_dir / "history.json").write_text('[{"admin_version": "v1.0.0"}]')

    paths.legacy_compose_backup_dir.mkdir(parents=True, exist_ok=True)
    (paths.legacy_compose_backup_dir / "docker-compose.admin.yml").write_text("services: {}\n")

    paths.legacy_appliance_log.write_text("web log\n")
    paths.legacy_operations_log.write_text("operation log\n")
    paths.legacy_audit_log.write_text('{"action": "login.success"}\n')
    return paths


def results(report):
    return {entry.source.rsplit("/", 1)[-1]: entry.result for entry in report.entries}


# --- fresh installation ----------------------------------------------------


def test_a_fresh_installation_has_nothing_to_migrate(tmp_path):
    paths = appliance_paths(tmp_path)
    report = migrate_state(paths)

    assert report.ok
    assert report.migrated == []
    assert set(results(report).values()) == {RESULT_SKIPPED}


def test_migration_creates_the_split_layout(tmp_path):
    paths = appliance_paths(tmp_path)
    migrate_state(paths)

    for directory in (paths.web_sessions_dir, paths.web_preferences_dir):
        assert directory.is_dir(), directory
    for directory in (paths.operations_dir, paths.known_good_dir, paths.compose_backup_dir):
        assert directory.is_dir(), directory
    assert paths.audit_log_dir.is_dir()


# --- migration from the shared layout --------------------------------------


def test_state_moves_to_its_owner(tmp_path):
    paths = legacy_installation(tmp_path)
    report = migrate_state(paths)

    assert report.ok, report.to_dict()
    assert paths.auth_file.is_file()
    assert (paths.operations_dir / ("a" * 32 + ".json")).is_file()
    assert (paths.known_good_dir / "history.json").is_file()
    assert (paths.compose_backup_dir / "docker-compose.admin.yml").is_file()
    assert paths.audit_log.is_file()


def test_authentication_is_preserved_byte_for_byte(tmp_path):
    paths = legacy_installation(tmp_path)
    before = paths.legacy_auth_file.read_text()
    migrate_state(paths)
    assert paths.auth_file.read_text() == before


def test_known_good_metadata_is_preserved(tmp_path):
    paths = legacy_installation(tmp_path)
    migrate_state(paths)
    history = json.loads((paths.known_good_dir / "history.json").read_text())
    assert history[0]["admin_version"] == "v1.0.0"


def test_operation_records_are_preserved(tmp_path):
    paths = legacy_installation(tmp_path)
    migrate_state(paths)
    records = list(paths.operations_dir.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["type"] == "admin.install"


def test_the_old_location_is_removed_only_after_the_copy_exists(tmp_path):
    paths = legacy_installation(tmp_path)
    migrate_state(paths)
    assert not paths.legacy_auth_file.exists()
    assert not paths.legacy_known_good_dir.exists()
    assert paths.auth_file.exists()
    assert (paths.known_good_dir / "history.json").exists()


# --- idempotence -----------------------------------------------------------


def test_running_the_migration_twice_changes_nothing(tmp_path):
    paths = legacy_installation(tmp_path)
    first = migrate_state(paths)
    content = paths.auth_file.read_text()

    second = migrate_state(paths)

    assert first.ok and second.ok
    assert second.migrated == []
    assert paths.auth_file.read_text() == content


def test_a_partially_completed_migration_finishes(tmp_path):
    paths = legacy_installation(tmp_path)
    # Only the auth file made it across before the previous run was interrupted.
    paths.auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.auth_file.write_text(paths.legacy_auth_file.read_text())
    paths.legacy_auth_file.unlink()

    report = migrate_state(paths)

    assert report.ok, report.to_dict()
    assert (paths.known_good_dir / "history.json").is_file()
    assert paths.auth_file.is_file()


# --- refusals and conflicts ------------------------------------------------


def test_a_symlinked_source_is_refused_not_followed(tmp_path):
    paths = appliance_paths(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-secret.json"
    outside.write_text("do not copy me")
    paths.legacy_auth_file.symlink_to(outside)

    report = migrate_state(paths)

    refused = [entry for entry in report.entries if entry.result == RESULT_REFUSED]
    assert refused, report.to_dict()
    assert not paths.auth_file.exists()
    assert outside.read_text() == "do not copy me"
    assert not report.ok


def test_a_destination_conflict_preserves_both_copies(tmp_path):
    paths = legacy_installation(tmp_path)
    paths.auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.auth_file.write_text('{"algorithm": "pbkdf2-sha256", "generation": "newer"}')

    report = migrate_state(paths)

    conflicts = [entry for entry in report.entries if entry.result == RESULT_CONFLICT]
    assert conflicts, report.to_dict()
    assert not report.ok
    assert "newer" in paths.auth_file.read_text()
    preserved = paths.auth_file.parent / (paths.auth_file.name + ".migrated-conflict")
    assert preserved.is_file()
    assert "abc" in preserved.read_text()


def test_identical_content_on_both_sides_is_not_a_conflict(tmp_path):
    paths = legacy_installation(tmp_path)
    paths.auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.auth_file.write_text(paths.legacy_auth_file.read_text())

    report = migrate_state(paths)

    auth_entries = [entry for entry in report.entries if entry.destination.endswith("auth.json")]
    assert auth_entries[0].result in (RESULT_ALREADY_DONE, RESULT_MIGRATED)
    assert report.ok


def test_a_migration_finding_is_reported_and_recorded(tmp_path):
    paths = legacy_installation(tmp_path)
    paths.auth_file.parent.mkdir(parents=True, exist_ok=True)
    paths.auth_file.write_text('{"generation": "newer"}')

    report = migrate_state(paths)
    target = write_report(paths, report)

    assert target is not None and target.is_file()
    recorded = json.loads(target.read_text())
    assert recorded["ok"] is False
    assert recorded["findings"]


# --- ownership -------------------------------------------------------------


def test_directory_modes_after_migration(tmp_path):
    import stat

    paths = legacy_installation(tmp_path)
    migrate_state(paths)

    assert stat.S_IMODE(paths.web_sessions_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.operations_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.audit_log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.agent_state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.agent_log_dir.stat().st_mode) == 0o700


def test_migrated_agent_files_are_root_only(tmp_path):
    import stat

    paths = legacy_installation(tmp_path)
    migrate_state(paths)

    for path in (paths.audit_log, paths.operations_log):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    for record in paths.known_good_dir.rglob("*"):
        if record.is_file():
            assert stat.S_IMODE(record.stat().st_mode) == 0o600, record


def test_no_auth_directory_is_created_under_the_web_state(tmp_path):
    """The password left this tree when it became shared: it lives in the EMS
    deployment root now, where Admin and the dashboard read it too. The web
    account owns no password store at all any more, so creating a private
    directory for one -- and enforcing its mode on every migration -- promises a
    secret that is not there and would send anyone looking to the wrong place.
    """

    paths = appliance_paths(tmp_path)
    migrate_state(paths)

    assert not (paths.web_state_dir / "auth").exists()
    assert not hasattr(paths, "web_auth_dir")
    assert paths.auth_file == paths.install_root / "config" / "dashboard-auth.json"


def test_migration_keeps_the_armed_reverter_executable(tmp_path):
    """State migration must not disarm the manager's way back.

    ``migrate_state`` normalises every file under agent state to 0600, and it
    runs on each agent start as root as well as from the postinst. The armed
    reverter lives there and systemd has to execute it, so a blanket mode
    silently removes the deadline that is the only way back out of a manager
    install.
    """

    paths = appliance_paths(tmp_path)
    reverter = paths.packages_dir / manager_verify.REVERTER_NAME
    reverter.parent.mkdir(parents=True, exist_ok=True)
    reverter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    reverter.chmod(manager_verify.REVERTER_MODE)

    ordinary = paths.packages_dir / "current.deb"
    ordinary.write_bytes(b"deb")
    ordinary.chmod(0o644)

    migrate_state(paths)

    # The normalisation itself must keep working.
    assert stat.S_IMODE(ordinary.stat().st_mode) == 0o600
    assert stat.S_IMODE(reverter.stat().st_mode) == manager_verify.REVERTER_MODE
    assert os.access(reverter, os.X_OK)


# --- a destination the web account chose -------------------------------------


def test_a_web_directory_replaced_by_a_symlink_does_not_hand_over_its_target(tmp_path):
    """The web account owns these directories, so it can replace one with a link.

    `migrate_state` then runs as root from the agent's start-up and from every
    postinst. `mkdir(exist_ok=True)` succeeds through the link, and the
    ownership pass that follows chowns and chmods whatever it points at --
    os.chown and Path.chmod both follow symlinks. That turns the process the
    architecture describes as having "no root and no way to run a host command"
    into a way to take any path on the appliance.
    """

    paths = appliance_paths(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir(parents=True)
    victim.chmod(0o700)
    secret = victim / "secret"
    secret.write_text("what only root may read\n")
    secret.chmod(0o600)

    planted = paths.web_preferences_dir
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(victim, target_is_directory=True)

    migrate_state(paths)

    assert stat.S_IMODE(victim.stat().st_mode) == 0o700, "the link target was re-moded"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600, "a file under the target was re-moded"


def test_an_agent_directory_replaced_by_a_symlink_does_not_hand_over_its_target(tmp_path):
    paths = appliance_paths(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir(parents=True)
    victim.chmod(0o755)

    planted = paths.operations_dir
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(victim, target_is_directory=True)

    migrate_state(paths)

    assert stat.S_IMODE(victim.stat().st_mode) == 0o755


def test_a_symlinked_destination_is_reported_rather_than_passed_over(tmp_path):
    """Silence would leave an operator with a link nothing will ever resolve."""

    paths = appliance_paths(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir(parents=True)
    planted = paths.web_preferences_dir
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(victim, target_is_directory=True)

    report = migrate_state(paths)

    assert [entry.source for entry in report.unsafe] == [str(planted)]
    assert "symlink" in report.unsafe[0].detail
    assert not report.ok, "a planted link is a finding, not a clean run"


def test_a_planted_link_does_not_also_block_every_later_install(tmp_path):
    """Whoever can plant one already holds the web account.

    `migrate-state` runs from the postinst under `|| fail`, so treating this as
    fatal would hand the same account a way to refuse every future package.
    Refusing to follow the link is the whole remedy; the appliance still runs.
    """

    paths = appliance_paths(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir(parents=True)
    planted = paths.web_preferences_dir
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(victim, target_is_directory=True)

    report = migrate_state(paths)

    assert not report.fatal, [entry.to_dict() for entry in report.fatal]


# --- the same question in the postinst ---------------------------------------


POSTINST = ROOT / "packaging" / "appliance" / "debian" / "postinst"


def _postinst_fragment(name):
    """One shell function, lifted out of the real maintainer script."""

    import re

    match = re.search(
        rf"^{name}\(\) \{{.*?^\}}$", POSTINST.read_text(encoding="utf-8"), re.DOTALL | re.MULTILINE
    )
    assert match, f"postinst no longer defines {name}()"
    return match.group(0)


def test_the_postinst_does_not_create_or_own_through_a_planted_link(tmp_path):
    """`mkdir -p` succeeds through a link and `chown` follows it.

    The same reach as the Python migration, from the script that runs first.
    """

    import subprocess

    victim = tmp_path / "victim"
    victim.mkdir()
    victim.chmod(0o700)
    planted = tmp_path / "ui-preferences"
    planted.symlink_to(victim, target_is_directory=True)

    script = "\n".join(
        [
            "set -e",
            'note() { echo "$1"; }',
            'fail() { echo "$1" >&2; exit 1; }',
            _postinst_fragment("ensure_directory"),
            f'ensure_directory "{planted}" && chmod 0750 "{planted}"',
            "exit 0",
        ]
    )
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(victim.stat().st_mode) == 0o700, "the link target was re-moded"
    assert planted.is_symlink(), "the link itself was removed rather than left alone"


def test_the_postinst_still_creates_an_ordinary_directory(tmp_path):
    import subprocess

    target = tmp_path / "web" / "ui-preferences"
    script = "\n".join(
        [
            "set -e",
            'note() { echo "$1"; }',
            'fail() { echo "$1" >&2; exit 1; }',
            _postinst_fragment("ensure_directory"),
            f'ensure_directory "{target}"',
        ]
    )
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert target.is_dir()


def test_every_managed_directory_goes_through_the_guard():
    """A loop added later must not reach for mkdir directly."""

    text = POSTINST.read_text(encoding="utf-8")
    body = text.split("case \"$1\" in", 1)[1]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("mkdir -p") and "$directory" in stripped:
            raise AssertionError(f"a managed directory bypasses ensure_directory: {stripped}")


def _harden_environment(tmp_path, *, failing_call):
    """A state tree, and a chmod that refuses exactly one call."""

    state = tmp_path / "state"
    log = tmp_path / "log"
    (state / "agent" / "packages").mkdir(parents=True)
    (state / "agent" / "packages" / "current.deb").write_bytes(b"a package")
    (log / "agent").mkdir(parents=True)
    (log / "audit").mkdir(parents=True)

    tools = tmp_path / "tools"
    tools.mkdir()
    attempts = tools / "attempts"
    (tools / "chmod").write_text(
        "#!/bin/sh\n"
        f'count=$(cat "{attempts}" 2>/dev/null || echo 0)\n'
        f'echo $((count + 1)) > "{attempts}"\n'
        f'if [ "$count" = "{failing_call}" ]; then\n'
        '  echo "chmod: cannot access: No such file or directory" >&2\n'
        "  exit 1\n"
        "fi\n"
        'exec /usr/bin/chmod "$@"\n',
        encoding="utf-8",
    )
    (tools / "chmod").chmod(0o755)
    (tools / "chown").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tools / "chown").chmod(0o755)
    return state, log, tools


def _run_harden(tmp_path, state, log, tools, fragments):
    """The shipped function, called the way line 144 calls it: bare, under set -e."""

    import subprocess

    script = "\n".join(
        [
            "set -e",
            'note() { echo "$1"; }',
            'fail() { echo "ems-appliance: $1" >&2; exit 1; }',
            f'STATE_DIR="{state}"',
            f'LOG_DIR="{log}"',
            "ARMED_REVERTER_NAME=verify-manager.armed.sh",
            *fragments,
            "harden_agent_state",
            "echo REACHED-THE-END",
        ]
    )
    environment = dict(os.environ)
    environment["PATH"] = f"{tools}:{environment['PATH']}"
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, env=environment, timeout=60
    )


def test_the_permissions_pass_survives_a_file_the_agent_replaced_under_it(tmp_path):
    """The agent this install replaces is still writing while this runs.

    `find -exec chmod +` collects names up to ARG_MAX and chmods them
    afterwards, so a staging name the running agent os.replace()d in between
    yields ENOENT and find exits non-zero. Called bare under `set -e`, that
    ended the postinst in the middle of the permissions pass -- with chmod's
    message and none of the project's own -- leaving the package
    half-configured, the services never restarted and verify-install never run.
    Measured against the pre-fix function: rc 1, no output, no `ems-appliance:`
    line.

    The race is modelled with a chmod that refuses one call, not by timing.
    """

    state, log, tools = _harden_environment(tmp_path, failing_call=1)

    result = _run_harden(
        tmp_path,
        state,
        log,
        tools,
        [_postinst_fragment("harden_once"), _postinst_fragment("harden_agent_state")],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REACHED-THE-END" in result.stdout


def test_a_permissions_failure_that_is_real_is_still_named(tmp_path):
    """The retry must not turn a genuine refusal into silence."""

    state, log, tools = _harden_environment(tmp_path, failing_call=0)
    (tools / "chmod").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (tools / "chmod").chmod(0o755)

    result = _run_harden(
        tmp_path,
        state,
        log,
        tools,
        [_postinst_fragment("harden_once"), _postinst_fragment("harden_agent_state")],
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot normalise the permissions" in result.stderr
