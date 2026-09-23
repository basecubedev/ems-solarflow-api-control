# SPDX-License-Identifier: AGPL-3.0-or-later
"""No verdict means revert.

Under A/B, an appliance that said nothing after an update rebooted into the
slot it came from: inaction was the safe answer. A package install has no such
property — inaction commits it. A deadline is the closest substitute available
and it is not equivalent, which is why what it does when it expires is written
down as behaviour rather than described.

The reverter is executed here, against fake host tools on ``PATH`` and a
temporary state directory, because a script nobody runs is a plan.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from appliance import artifact_trust, manager_verify
from appliance import paths as appliance_paths

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"
REVERTER = PACKAGING / "bin" / "verify-manager.sh"

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]


class FakeResult:
    def __init__(self, ok=True, stdout="", stderr=""):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, *, ok=True, available=True):
        self.calls = []
        self._ok = ok
        self._available = available

    def available(self, tool):
        return self._available

    def run(self, tool, args, timeout=None):
        self.calls.append((tool, list(args)))
        return FakeResult(ok=self._ok, stderr="" if self._ok else "refused")


class FakePaths:
    def __init__(self, root):
        self.packages_dir = Path(root) / "packages"


@pytest.fixture
def paths(tmp_path):
    fake = FakePaths(tmp_path)
    fake.packages_dir.mkdir(parents=True)
    return fake


@pytest.fixture
def packaged(tmp_path):
    """A stand-in for the reverter the outgoing package has on disk."""

    target = tmp_path / "packaged" / "verify-manager.sh"
    target.parent.mkdir(parents=True)
    target.write_text(REVERTER.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    return target


def arm(paths, packaged, runner, **kwargs):
    return manager_verify.arm(
        paths,
        runner,
        expected_version=kwargs.pop("expected_version", "0.2.0"),
        build_id=kwargs.pop("build_id", "20260826010000"),
        previous=kwargs.pop("previous", str(paths.packages_dir / "previous.deb")),
        now=kwargs.pop("now", 1_000_000),
        reverter=kwargs.pop("reverter", str(packaged)),
        **kwargs,
    )


# --- arming ------------------------------------------------------------------


def test_arming_snapshots_the_reverter_out_of_the_package_that_is_being_replaced(
    paths, packaged
):
    arm(paths, packaged, FakeRunner())

    snapshot = manager_verify.reverter_path(paths)
    assert snapshot.is_file(), "the packaged copy is replaced by the install it must judge"
    assert snapshot.read_text(encoding="utf-8") == packaged.read_text(encoding="utf-8")
    assert stat.S_IMODE(snapshot.stat().st_mode) & stat.S_IXUSR


def test_the_unit_runs_the_snapshot_and_not_the_packaged_copy():
    unit = (PACKAGING / "systemd" / "ems-appliance-manager-verify.service").read_text(
        encoding="utf-8"
    )

    assert manager_verify.REVERTER_NAME in unit
    assert "/usr/lib/ems-appliance-manager/verify-manager.sh" not in unit


def installed_packages_dir():
    """Where ``arm()`` puts the snapshot on an appliance the package installed."""

    return appliance_paths.AppliancePaths(
        install_root=Path(appliance_paths.DEFAULT_INSTALL_ROOT),
        config_dir=Path(appliance_paths.DEFAULT_CONFIG_DIR),
        state_dir=Path(appliance_paths.DEFAULT_STATE_DIR),
        log_dir=Path(appliance_paths.DEFAULT_LOG_DIR),
        runtime_dir=Path(appliance_paths.DEFAULT_RUNTIME_DIR),
    ).packages_dir


def test_the_unit_names_the_directory_arming_actually_writes_to():
    """systemd cannot expand a variable in the program path, so the unit spells
    it out -- and a spelled-out path is one that drifts silently. It named the
    pre-split directory, which nothing has written to since ``packages_dir``
    moved under ``agent/``: the timer would have run every minute, found no
    reverter, and never undone anything."""

    unit = (PACKAGING / "systemd" / "ems-appliance-manager-verify.service").read_text(
        encoding="utf-8"
    )
    directory = str(installed_packages_dir())

    assert (
        f"ExecStart=/bin/sh {directory}/{manager_verify.REVERTER_NAME} {directory}\n"
        in unit
    )
    assert f"{appliance_paths.DEFAULT_STATE_DIR}/packages/" not in unit, (
        "that is the legacy directory migration.py moves away from"
    )


def test_the_reverter_falls_back_to_the_directory_it_is_installed_beside():
    script = REVERTER.read_text(encoding="utf-8")

    assert f":-{installed_packages_dir()}}}" in script


def test_arming_starts_the_timer(paths, packaged):
    runner = FakeRunner()

    arm(paths, packaged, runner)

    assert runner.calls == [("systemctl", ["enable", "--now", manager_verify.VERIFY_TIMER])]


def test_a_timer_that_will_not_start_leaves_no_deadline_armed(paths, packaged):
    """The deadline is written before the timer on purpose -- a timer that fired
    first would find nothing to judge. But a deadline with no timer judges
    nothing and blocks everything: the console gates both Update and Revert on
    ``armed``, so the appliance would sit there offering neither, with the
    package it just installed and no way to act on it."""

    runner = FakeRunner(ok=False, available=True)

    with pytest.raises(manager_verify.ManagerVerifyError) as refusal:
        arm(paths, packaged, runner)

    assert refusal.value.code == "verify_timer_failed"
    assert not manager_verify.deadline_path(paths).exists()


def test_a_host_without_systemctl_leaves_no_deadline_armed(paths, packaged):
    """Same wedge, reached without running anything at all."""

    runner = FakeRunner(available=False)

    with pytest.raises(manager_verify.ManagerVerifyError) as refusal:
        arm(paths, packaged, runner)

    assert refusal.value.code == "systemctl_unavailable"
    assert not manager_verify.deadline_path(paths).exists()


def test_without_a_reverter_to_snapshot_nothing_is_armed(paths, tmp_path):
    runner = FakeRunner()

    with pytest.raises(manager_verify.ManagerVerifyError) as refusal:
        arm(paths, None, runner, reverter=str(tmp_path / "absent.sh"))

    assert refusal.value.code == "reverter_missing"
    assert not manager_verify.deadline_path(paths).exists()
    assert runner.calls == [], "an unarmed deadline must not leave a timer running"


def test_the_deadline_records_what_the_install_is_expected_to_produce(paths, packaged):
    arm(paths, packaged, FakeRunner(), now=1_000_000, window_seconds=900)

    record = manager_verify.read(paths)

    assert record.armed
    assert record.expected_version == "0.2.0"
    assert record.deadline_epoch == 1_000_900
    assert not record.expired(1_000_899)
    assert record.expired(1_000_900)


def test_an_install_with_nothing_to_go_back_to_says_so_rather_than_refusing(paths, packaged):
    """A first install has no earlier package, and must still be possible."""

    arm(paths, packaged, FakeRunner(), previous="")

    record = manager_verify.read(paths)

    assert record.armed
    assert not record.revert_available


def test_disarming_removes_the_deadline_and_stops_the_timer(paths, packaged):
    arm(paths, packaged, FakeRunner())
    runner = FakeRunner()

    manager_verify.disarm(paths, runner)

    assert not manager_verify.deadline_path(paths).exists()
    assert runner.calls == [("systemctl", ["disable", "--now", manager_verify.VERIFY_TIMER])]


def test_an_unreadable_deadline_is_reported_rather_than_read_as_unarmed(paths):
    manager_verify.deadline_path(paths).write_text("{not json", encoding="utf-8")

    record = manager_verify.read(paths)

    assert not record.armed
    assert record.unreadable


def test_the_verdict_the_reverter_leaves_is_read_back(paths):
    manager_verify.verdict_path(paths).write_text(
        json.dumps({"verdict": "reverted", "detail": "d", "decided_at": "t"}), encoding="utf-8"
    )

    verdict = manager_verify.read_verdict(paths)

    assert verdict.verdict == "reverted"
    assert verdict.settled


# --- the reverter itself -----------------------------------------------------


def fake_tools(
    directory,
    *,
    installed_version,
    installed_status="installed",
    agent="active",
    web="active",
    dpkg_exit=0,
):
    """A PATH whose systemctl, dpkg and dpkg-query answer as instructed.

    The dpkg-query fake honours the format string it is handed. One that prints
    the version whatever it was asked for cannot tell a package dpkg calls
    installed from one it calls half-configured, which is the distinction the
    reverter turns on.
    """

    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "calls.log"
    query = (
        "#!/bin/sh\n"
        'echo "dpkg-query $*" >> "LOG_PATH"\n'
        "fmt=\n"
        "want=\n"
        'for arg in "$@"; do\n'
        '  if [ "$want" = yes ]; then fmt=$arg; want=; continue; fi\n'
        '  [ "$arg" = "-f" ] && want=yes\n'
        "done\n"
        'printf %s "$fmt" | sed -e "s/[$]{Version}/VERSION/g" '
        '-e "s/[$]{db:Status-Status}/STATUS/g"\n'
    )
    (directory / "dpkg-query").write_text(
        query.replace("LOG_PATH", str(log))
        .replace("VERSION", installed_version)
        .replace("STATUS", installed_status),
        encoding="utf-8",
    )
    (directory / "dpkg").write_text(
        f'#!/bin/sh\necho "dpkg $*" >> "{log}"\nexit {dpkg_exit}\n', encoding="utf-8"
    )
    (directory / "systemctl").write_text(
        "#!/bin/sh\n"
        f'echo "systemctl $*" >> "{log}"\n'
        'case "$*" in\n'
        f'  *ems-appliance-agent*) [ "{agent}" = active ] || exit 3 ;;\n'
        f'  *ems-appliance-web*) [ "{web}" = active ] || exit 3 ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for name in ("dpkg-query", "dpkg", "systemctl"):
        (directory / name).chmod(0o755)
    return log


def run_reverter(paths, tools, *, now):
    environment = dict(os.environ)
    environment["PATH"] = f"{tools}:{environment['PATH']}"
    # The reverter reads the clock through `date`, which the fake PATH does not
    # shadow: a test that fixed the clock by shadowing it would be testing the
    # fake. The deadline is moved instead.
    return subprocess.run(
        ["sh", str(REVERTER), str(paths.packages_dir)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )


def deadline_at(paths, packaged, *, epoch, previous=True, version="0.2.0"):
    archive = paths.packages_dir / "previous.deb"
    if previous:
        archive.write_bytes(b"an earlier package")
    arm(
        paths,
        packaged,
        FakeRunner(),
        expected_version=version,
        previous=str(archive) if previous else "",
        now=epoch - 900,
        window_seconds=900,
    )


def test_a_healthy_install_confirms_itself_before_the_deadline(paths, packaged, tmp_path):
    deadline_at(paths, packaged, epoch=4_000_000_000)
    tools = tmp_path / "tools"
    log = fake_tools(tools, installed_version="0.2.0")

    result = run_reverter(paths, tools, now=0)

    assert result.returncode == 0, result.stderr
    assert manager_verify.read_verdict(paths).verdict == manager_verify.VERDICT_CONFIRMED
    assert not manager_verify.deadline_path(paths).exists()
    assert "dpkg --force-confold" not in log.read_text(encoding="utf-8")
    assert "disable --now" in log.read_text(encoding="utf-8")


def test_an_expired_deadline_without_a_verdict_puts_the_previous_package_back(
    paths, packaged, tmp_path
):
    deadline_at(paths, packaged, epoch=1)
    tools = tmp_path / "tools"
    log = fake_tools(tools, installed_version="0.2.0", agent="failed")

    result = run_reverter(paths, tools, now=0)

    assert result.returncode == 0, result.stderr
    assert manager_verify.read_verdict(paths).verdict == manager_verify.VERDICT_REVERTED
    assert "previous.deb" in log.read_text(encoding="utf-8")
    assert not manager_verify.deadline_path(paths).exists()


def test_an_unhealthy_install_inside_the_window_waits_rather_than_deciding(
    paths, packaged, tmp_path
):
    deadline_at(paths, packaged, epoch=4_000_000_000)
    tools = tmp_path / "tools"
    log = fake_tools(tools, installed_version="0.2.0", web="failed")

    result = run_reverter(paths, tools, now=0)

    assert result.returncode == 0, result.stderr
    assert not manager_verify.read_verdict(paths).settled
    assert manager_verify.deadline_path(paths).exists(), "the window has not run out yet"
    assert "previous.deb" not in log.read_text(encoding="utf-8")


def test_a_package_that_is_not_the_one_the_install_promised_is_never_confirmed(
    paths, packaged, tmp_path
):
    deadline_at(paths, packaged, epoch=1, version="0.3.0")
    tools = tmp_path / "tools"
    fake_tools(tools, installed_version="0.2.0")

    run_reverter(paths, tools, now=0)

    assert manager_verify.read_verdict(paths).verdict == manager_verify.VERDICT_REVERTED


def test_an_expired_deadline_with_nothing_to_revert_to_asks_for_a_person(
    paths, packaged, tmp_path
):
    deadline_at(paths, packaged, epoch=1, previous=False)
    tools = tmp_path / "tools"
    fake_tools(tools, installed_version="0.9.9")

    run_reverter(paths, tools, now=0)

    verdict = manager_verify.read_verdict(paths)
    assert verdict.verdict == manager_verify.VERDICT_UNAVAILABLE
    assert verdict.settled


def test_a_package_dpkg_has_not_configured_is_never_confirmed(paths, packaged, tmp_path):
    """`${Version}` answers for a package dpkg never finished configuring.

    A postinst that dies after restarting the two units leaves the new version
    unpacked and half-configured, with both services up. Asking only for the
    version reads that as the install the deadline was armed for, confirms it,
    and throws away the one automatic way back -- so the check has to ask dpkg
    what state the package is in, not merely which version it knows about.
    """

    deadline_at(paths, packaged, epoch=1)
    tools = tmp_path / "tools"
    fake_tools(tools, installed_version="0.2.0", installed_status="half-configured")

    run_reverter(paths, tools, now=0)

    verdict = manager_verify.read_verdict(paths)
    assert verdict.verdict != manager_verify.VERDICT_CONFIRMED, verdict.detail
    assert verdict.verdict == manager_verify.VERDICT_REVERTED


def test_a_package_dpkg_only_has_config_files_for_is_never_confirmed(paths, packaged, tmp_path):
    """dpkg-query still reports a version for a package that is no longer on."""

    deadline_at(paths, packaged, epoch=1)
    tools = tmp_path / "tools"
    fake_tools(tools, installed_version="0.2.0", installed_status="config-files")

    run_reverter(paths, tools, now=0)

    assert manager_verify.read_verdict(paths).verdict != manager_verify.VERDICT_CONFIRMED


def test_a_revert_dpkg_refuses_once_is_tried_again_on_the_next_tick(paths, packaged, tmp_path):
    """The lock another dpkg holds is gone a tick later; the deadline is not.

    An operator repairing the package manager from the console holds the dpkg
    frontend lock for as long as apt runs. Settling on revert_failed at the
    first refusal spends the only automatic way back on a condition that clears
    itself, and disarming takes away the tick that would have retried.
    """

    deadline_at(paths, packaged, epoch=1)
    tools = tmp_path / "tools"
    fake_tools(tools, installed_version="0.9.9", dpkg_exit=1)

    run_reverter(paths, tools, now=0)

    assert not manager_verify.read_verdict(paths).settled
    assert manager_verify.deadline_path(paths).exists(), "the next tick has to be able to retry"


def test_a_revert_that_keeps_being_refused_settles_rather_than_ticking_forever(
    paths, packaged, tmp_path
):
    deadline_at(paths, packaged, epoch=1)
    tools = tmp_path / "tools"
    fake_tools(tools, installed_version="0.9.9", dpkg_exit=1)

    for _ in range(manager_verify.REVERT_ATTEMPTS):
        run_reverter(paths, tools, now=0)

    assert manager_verify.read_verdict(paths).verdict == manager_verify.VERDICT_REVERT_FAILED
    assert not manager_verify.deadline_path(paths).exists()


def test_a_retried_revert_that_succeeds_is_recorded_as_a_revert(paths, packaged, tmp_path):
    """The attempt counter must not outlive the deadline it belongs to."""

    deadline_at(paths, packaged, epoch=1)
    tools = tmp_path / "tools"
    fake_tools(tools, installed_version="0.9.9", dpkg_exit=1)
    run_reverter(paths, tools, now=0)

    fake_tools(tools, installed_version="0.9.9", dpkg_exit=0)
    run_reverter(paths, tools, now=0)

    assert manager_verify.read_verdict(paths).verdict == manager_verify.VERDICT_REVERTED
    assert not manager_verify.attempts_path(paths).exists()


def test_a_tick_with_no_deadline_disarms_itself(paths, tmp_path):
    tools = tmp_path / "tools"
    log = fake_tools(tools, installed_version="0.2.0")

    result = run_reverter(paths, tools, now=0)

    assert result.returncode == 0, result.stderr
    assert "disable --now" in log.read_text(encoding="utf-8")


# --- properties that live outside Python -------------------------------------


def test_the_reverter_imports_nothing_the_install_replaces():
    commands = "\n".join(
        line
        for line in REVERTER.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )

    assert "python" not in commands.lower()
    assert "ems-appliance " not in commands
    assert "appliance." not in commands


def test_the_deadline_survives_a_reboot_inside_the_window():
    """A one-shot timer that a reboot cancels is a deadline that never expires."""

    timer = (PACKAGING / "systemd" / "ems-appliance-manager-verify.timer").read_text(
        encoding="utf-8"
    )

    assert "OnBootSec=" in timer
    assert "OnUnitActiveSec=" in timer
    assert "[Install]" in timer, "arming enables the timer, so it needs an install section"


def test_the_package_ships_the_reverter_and_its_units():
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")

    assert "verify-manager.sh" in build
    assert "ems-appliance-manager-verify.service" in build
    assert "ems-appliance-manager-verify.timer" in build


def test_the_timer_is_not_enabled_by_the_package():
    """An always-running deadline would revert an appliance nobody updated."""

    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")

    assert "ems-appliance-manager-verify.timer" not in postinst


def test_the_deadline_does_not_hang_on_the_snapshot_being_executable():
    """The execute bit is not something this unit may depend on.

    Three separate things normalise the mode of everything under agent state --
    the postinst, ``migration._apply_ownership`` and, on a revert, an older
    package that carries no fix at all. Running the snapshot through its
    interpreter takes the bit out of the load path wherever *this* unit is the
    one installed.

    It does not rescue a revert: that reinstalls the older unit too, which
    resolves the snapshot as a program. Only the restore in
    ``install-manager.sh`` covers that, and the ADR records it as a limit.
    """

    unit = (PACKAGING / "systemd" / "ems-appliance-manager-verify.service").read_text(
        encoding="utf-8"
    )
    exec_start = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_start.startswith("ExecStart=/bin/sh "), exec_start

    # The snapshot is a /bin/sh script, so the interpreter has to be that one.
    packaged = (PACKAGING / "bin" / "verify-manager.sh").read_text(encoding="utf-8")
    assert packaged.splitlines()[0] == "#!/bin/sh", packaged.splitlines()[0]


def test_the_reverter_asks_dpkg_the_question_this_module_names():
    """One authority for how "is it installed" is asked, two readers of it."""

    script = REVERTER.read_text(encoding="utf-8")
    assert f"-f '{manager_verify.DPKG_STATE_QUERY}'" in script
    assert f"= {manager_verify.DPKG_STATE_INSTALLED}" in script
    assert f"REVERT_ATTEMPTS={manager_verify.REVERT_ATTEMPTS}" in script
    assert f'ATTEMPTS="$STATE/{manager_verify.ATTEMPTS_NAME}"' in script


def test_disarming_takes_the_attempt_count_with_it():
    """A count that outlived its deadline would shorten the next one."""

    script = REVERTER.read_text(encoding="utf-8")
    match = re.search(r"^disarm\(\) \{.*?^\}$", script, re.DOTALL | re.MULTILINE)
    assert match, "verify-manager.sh no longer defines disarm()"
    assert '"$ATTEMPTS"' in match.group(0)


def test_a_refused_revert_is_cured_before_it_is_tried_again(paths, packaged, tmp_path):
    """The interrupted dpkg the revert trips over is the one it can repair.

    `ems-appliance-manager-install.service` has TimeoutStartSec=900 and the
    deadline window is 900 s, so both run out in the same moment: systemd
    SIGTERMs the install cgroup with dpkg inside it and the database is left
    interrupted. The revert then gets the usual refusal, and running
    `dpkg --configure -a` only after the attempts are spent means every retry
    fails for a reason the script already knows how to clear.
    `install-manager.sh` does it the other way round and says why.
    """

    deadline_at(paths, packaged, epoch=1)
    tools = tmp_path / "tools"
    log = fake_tools(tools, installed_version="0.9.9", dpkg_exit=1)

    run_reverter(paths, tools, now=0)

    calls = log.read_text(encoding="utf-8")
    assert "dpkg --configure -a" in calls, calls
    assert not manager_verify.read_verdict(paths).settled, "the tick must still leave a retry"
def test_an_archive_swapped_under_an_armed_deadline_is_refused(paths, packaged, tmp_path):
    """`previous.deb` is a slot, and retain() rewrites it on every install.

    A second install while a deadline is unjudged rotates the unjudged package
    into that slot. The deadline then names a path holding the very package it
    was armed to undo, and reverting to it reports success for reinstalling the
    problem.
    """

    deadline_at(paths, packaged, epoch=1)
    kept = paths.packages_dir / "previous.deb"
    kept.write_bytes(b"the package the deadline was armed to undo")

    tools = tmp_path / "tools"
    log = fake_tools(tools, installed_version="0.9.9")

    run_reverter(paths, tools, now=0)

    assert "previous.deb" not in log.read_text(encoding="utf-8"), "a swapped archive was installed"
    assert manager_verify.read_verdict(paths).verdict == manager_verify.VERDICT_UNAVAILABLE
    assert not manager_verify.deadline_path(paths).exists()


def test_the_deadline_records_what_the_kept_archive_held_when_it_armed(paths, packaged):
    kept = paths.packages_dir / "previous.deb"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"an earlier package")

    deadline, _ = arm(paths, packaged, FakeRunner(), previous=str(kept))

    assert deadline.previous_sha256 == artifact_trust.file_digest(kept)
    assert manager_verify.read(paths).previous_sha256 == deadline.previous_sha256


def test_a_deadline_armed_with_nothing_kept_records_no_digest(paths, packaged):
    deadline, _ = arm(paths, packaged, FakeRunner(), previous="")

    assert deadline.previous_sha256 == ""
