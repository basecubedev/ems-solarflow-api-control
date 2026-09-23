# SPDX-License-Identifier: AGPL-3.0-or-later
"""Installing the package the installer is running from.

Three properties are being defended, and each one exists because the obvious
implementation gets it wrong:

The refusals happen before dpkg runs, while this project's Python is still the
code that started the process. Afterwards the files under
``/usr/lib/ems-appliance-manager/appliance`` are the *new* ones, so any decision
taken later is taken by code nobody has proven yet.

`dpkg` runs in its own cgroup. As a child of the agent it is killed by the
package's own postinst restarting that agent — and the documented cure,
`dpkg --configure -a`, re-runs the same postinst from the same cgroup and dies
identically, which is how a failed update becomes a state no remote action can
leave.

The agent does not wait for the outcome. It cannot: it is restarted by the
install it started. The verdict arrives in a file.
"""

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

from appliance import manager_install, manager_releases, manager_retention, manager_verify, persistent_state
from appliance import paths as appliance_paths

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

DIGEST_BODY = b"a package"


class FakePaths:
    def __init__(self, root):
        self.packages_dir = Path(root) / "packages"
        self.persistent_mountpoint = ""


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


@pytest.fixture
def paths(tmp_path):
    return FakePaths(tmp_path)


def archive(tmp_path, body=DIGEST_BODY):
    target = tmp_path / "ems-appliance-manager_0.2.0_arm64.deb"
    target.write_bytes(body)
    return target


def release(tmp_path, *, body=DIGEST_BODY, verified=manager_releases.VERIFIED_SIGNATURE, **over):
    import hashlib

    payload = {
        "format_version": manager_releases.MANIFEST_FORMAT_VERSION,
        "package": "ems-appliance-manager",
        "version": "0.2.0",
        "architecture": "arm64",
        "build_id": "20260826010000",
        "created_at": "2026-08-26T01:00:00Z",
        "project_revision": "a" * 40,
        "artifact": {
            "name": "ems-appliance-manager_0.2.0_arm64.deb",
            "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body),
        },
        "state_schemas": manager_releases.implemented_state_schemas(),
    }
    payload.update(over)
    return manager_releases.parse_manifest(payload, verified=verified)


def prepare(paths, tmp_path, **kwargs):
    return manager_install.prepare(
        paths,
        release=kwargs.pop("release", None) or release(tmp_path),
        archive=kwargs.pop("archive", None) or archive(tmp_path),
        state_schemas=kwargs.pop("state_schemas", persistent_state.implemented_schemas()),
        **kwargs,
    )


def test_a_verified_package_is_retained_and_staged(paths, tmp_path):
    prepare(paths, tmp_path)

    request = json.loads(manager_install.request_path(paths).read_text(encoding="utf-8"))
    kept = manager_retention.read(paths)

    assert request["version"] == "0.2.0"
    assert Path(request["archive"]).is_file()
    assert kept.current.present, "the outgoing package is kept before anything is unpacked"


def test_an_unsigned_package_is_refused_before_anything_is_written(paths, tmp_path):
    with pytest.raises(manager_install.ManagerInstallError) as refusal:
        prepare(paths, tmp_path, release=release(tmp_path, verified=manager_releases.VERIFIED_NONE))

    assert refusal.value.code == "manager_not_signed"
    assert not manager_install.request_path(paths).exists()


def test_an_archive_that_is_not_the_one_the_manifest_names_is_refused(paths, tmp_path):
    other = tmp_path / "other.deb"
    other.write_bytes(b"something else")

    with pytest.raises(manager_releases.ManagerReleaseError):
        prepare(paths, tmp_path, archive=other)

    assert not manager_install.request_path(paths).exists()


def test_an_appliance_that_cannot_say_what_its_state_is_refuses(paths, tmp_path):
    with pytest.raises(manager_install.ManagerInstallError) as refusal:
        prepare(paths, tmp_path, state_schemas=None)

    assert refusal.value.code == "state_schemas_unrecorded"
    assert not manager_install.request_path(paths).exists()


def test_a_package_that_could_not_read_this_state_is_refused(paths, tmp_path):
    ahead = {k: v + 1 for k, v in persistent_state.implemented_schemas().items()}

    with pytest.raises(manager_install.ManagerInstallError) as refusal:
        prepare(paths, tmp_path, state_schemas=ahead)

    assert refusal.value.code == "artifact_state_schema_too_old"


def test_a_stale_result_is_cleared_so_the_last_answer_is_not_reused(paths, tmp_path):
    paths.packages_dir.mkdir(parents=True, exist_ok=True)
    manager_install.result_path(paths).write_text('{"outcome": "installed"}', encoding="utf-8")

    prepare(paths, tmp_path)

    assert not manager_install.result_path(paths).exists()
    assert not manager_install.read_outcome(paths).settled


def test_the_agent_starts_the_unit_and_does_not_wait():
    runner = FakeRunner()

    manager_install.start(runner)

    assert runner.calls == [
        ("systemctl", ["start", "--no-block", manager_install.INSTALL_UNIT])
    ], "waiting would mean waiting inside the process the install restarts"


def test_a_unit_that_will_not_start_is_reported(paths):
    with pytest.raises(manager_install.ManagerInstallError) as refusal:
        manager_install.start(FakeRunner(ok=False))

    assert refusal.value.code == "install_unit_failed"


def test_without_systemctl_no_install_begins():
    with pytest.raises(manager_install.ManagerInstallError) as refusal:
        manager_install.start(FakeRunner(available=False))

    assert refusal.value.code == "systemctl_unavailable"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"outcome": "installed", "detail": "x"}', "installed"),
        ('{"outcome": "reverted", "detail": "y"}', "reverted"),
        ("{not json", manager_install.OUTCOME_PENDING),
        ("[]", manager_install.OUTCOME_PENDING),
    ],
)
def test_the_outcome_is_read_from_the_file_the_unit_writes(paths, payload, expected):
    paths.packages_dir.mkdir(parents=True, exist_ok=True)
    manager_install.result_path(paths).write_text(payload, encoding="utf-8")

    assert manager_install.read_outcome(paths).outcome == expected


# --- the two properties that live outside Python -----------------------------


def test_the_installer_runs_in_its_own_cgroup():
    unit = (PACKAGING / "systemd" / "ems-appliance-manager-install.service").read_text(
        encoding="utf-8"
    )

    assert "ExecStart=/usr/lib/ems-appliance-manager/install-manager.sh" in unit
    assert "ems-appliance-agent.service" not in unit.split("[Service]")[0].replace("#", "", 1) or (
        "After=" not in unit
    ), "an ordering edge would put the installer back inside the lifecycle it stands outside of"


def test_the_installer_imports_nothing_that_the_install_replaces():
    """dpkg rewrites appliance/*.py underneath a running interpreter."""

    script = (PACKAGING / "bin" / "install-manager.sh").read_text(encoding="utf-8")

    # Comments may name what is being avoided; the commands may not use it.
    commands = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("#")
    )
    assert "ems-appliance " not in commands, "the CLI is the code being replaced"
    assert "python" not in commands.lower()


def installed_paths():
    """What ``AppliancePaths`` resolves on an appliance the package installed.

    ``postinst`` hard-codes the same state directory, so this is not one of
    several possible answers -- it is the only one.
    """

    return appliance_paths.AppliancePaths(
        install_root=Path(appliance_paths.DEFAULT_INSTALL_ROOT),
        config_dir=Path(appliance_paths.DEFAULT_CONFIG_DIR),
        state_dir=Path(appliance_paths.DEFAULT_STATE_DIR),
        log_dir=Path(appliance_paths.DEFAULT_LOG_DIR),
        runtime_dir=Path(appliance_paths.DEFAULT_RUNTIME_DIR),
    )


def test_the_installer_reads_the_directory_the_agent_writes():
    """The request is written by Python and read by shell, and nothing joined them.

    ``paths.packages_dir`` moved under ``agent/`` with the privilege split, and
    the shell kept the pre-split path. Every unit test passes its own temporary
    directory to both halves, so the two never had to agree -- while on a real
    appliance the installer would find no request at all and the feature would
    be inert.
    """

    script = (PACKAGING / "bin" / "install-manager.sh").read_text(encoding="utf-8")
    expected = str(installed_paths().packages_dir)

    assert f"STATE={expected}\n" in script, (
        f"install-manager.sh must read {expected}, the directory "
        "manager_install.stage() writes into"
    )
    commands = "\n".join(line for line in script.splitlines() if not line.strip().startswith("#"))
    assert str(appliance_paths.DEFAULT_STATE_DIR) + "/packages" not in commands, (
        "that is the legacy directory migration.py moves away from"
    )


def test_the_postinst_creates_the_directory_the_installer_reads():
    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    relative = str(installed_paths().packages_dir).replace(
        str(appliance_paths.DEFAULT_STATE_DIR), "$STATE_DIR"
    )

    assert f'"{relative}"' in postinst, f"{relative} is never created"


def test_the_installer_refuses_an_archive_from_outside_its_own_directory():
    script = (PACKAGING / "bin" / "install-manager.sh").read_text(encoding="utf-8")

    assert 'case "$ARCHIVE" in' in script
    assert "is outside" in script


def test_the_installer_puts_the_previous_package_back_when_dpkg_fails():
    script = (PACKAGING / "bin" / "install-manager.sh").read_text(encoding="utf-8")

    assert "dpkg --configure -a" in script, "try the ordinary cure before reaching for the revert"
    assert "reverted" in script
    assert "revert_failed" in script


def test_the_package_ships_both_halves():
    build = (PACKAGING / "build-deb.sh").read_text(encoding="utf-8")

    assert "ems-appliance-manager-install.service" in build
    assert "install-manager.sh" in build


def test_the_update_path_imports_eagerly():
    """A lazily imported error handler is the code that gets replaced.

    dpkg swaps the module files while the interpreter runs, so anything this
    path defers until failure would be loaded *after* the unpack — new code
    deciding what to do about an install nobody has proven yet.
    """

    source = (ROOT / "appliance" / "manager_install.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and "appliance" in stripped:
            assert not line.startswith((" ", "\t")), f"deferred import: {stripped}"


# --- going back has one owner ------------------------------------------------


def test_a_revert_rotates_what_is_kept_rather_than_leaving_it(paths, tmp_path):
    """Both revert paths must agree about which package is current.

    The console revert rotated and the console CLI did not, so after a console
    rollback the browser named the package that was *running* as the one kept
    to go back to — and a second rollback reinstalled what was already there.
    """

    prepare(paths, tmp_path)
    newer = tmp_path / "newer.deb"
    newer.write_bytes(b"a newer package")
    manager_retention.retain(
        paths,
        newer,
        sha256="sha256:" + __import__("hashlib").sha256(b"a newer package").hexdigest(),
        version="0.3.0",
        build_id="20260901000000",
    )

    target, retention = manager_install.prepare_revert(paths, retained_at="t")

    assert target.version == "0.2.0"
    assert retention.current.version == "0.2.0", "the package going back on is now current"
    assert retention.previous.version == "0.3.0", "and the one being left is what undoes it"


def test_a_revert_names_the_rotated_copy_and_not_the_slot_it_came_from(paths, tmp_path):
    """previous.deb is overwritten by the rotation.

    An install request that named it directly would put back the very package
    it is leaving.
    """

    prepare(paths, tmp_path)
    newer = tmp_path / "newer.deb"
    newer.write_bytes(b"a newer package")
    manager_retention.retain(paths, newer, sha256="sha256:" + "b" * 64, version="0.3.0")

    _, retention = manager_install.prepare_revert(paths, retained_at="t")

    request = json.loads(manager_install.request_path(paths).read_text(encoding="utf-8"))
    assert request["archive"] == retention.current.path
    assert Path(request["archive"]).name == manager_retention.CURRENT_NAME
    assert request["version"] == "0.2.0"


def test_a_kept_package_that_no_longer_hashes_to_its_record_is_refused(paths, tmp_path):
    prepare(paths, tmp_path)
    newer = tmp_path / "newer.deb"
    newer.write_bytes(b"a newer package")
    manager_retention.retain(paths, newer, sha256="sha256:" + "b" * 64, version="0.3.0")
    (paths.packages_dir / manager_retention.PREVIOUS_NAME).write_bytes(b"tampered")

    with pytest.raises(manager_releases.ManagerReleaseError) as refusal:
        manager_install.prepare_revert(paths, retained_at="t")

    kept = manager_retention.read(paths)
    assert refusal.value.code == "manager_artifact_corrupt"
    # Refused before anything moved: the record still says what it said, so the
    # appliance has not been told it went back when it did not.
    assert kept.current.version == "0.3.0"
    assert kept.previous.version == "0.2.0"


def test_a_revert_with_nothing_kept_refuses_before_it_writes(paths):
    with pytest.raises(manager_retention.RetentionError) as refusal:
        manager_install.prepare_revert(paths, retained_at="t")

    assert refusal.value.code == "no_previous_package"


def test_both_revert_paths_go_through_the_same_owner():
    """A console rollback and a browser rollback are one mutation, not two."""

    service = (ROOT / "appliance" / "manager_update.py").read_text(encoding="utf-8")
    cli = (ROOT / "appliance" / "cli.py").read_text(encoding="utf-8")

    assert "manager_install.prepare_revert(" in service
    assert "manager_install.prepare_revert(" in cli
    # Neither may reach past it into the record itself.
    assert "manager_retention.retain(" not in cli


def test_the_console_rollback_keeps_an_operators_conffile():
    """A tty-less dpkg cannot answer a conffile prompt, so it must not be asked."""

    cli = (ROOT / "appliance" / "cli.py").read_text(encoding="utf-8")
    section = cli.split("def command_rollback_manager(", 1)[1].split("\ndef ", 1)[0]

    assert "--force-confold" in section


def test_the_staging_copy_is_never_named_from_the_record():
    """A path built from a manifest field is a path a manifest can steer.

    The build id is validated where a manifest becomes trusted, and this does
    not depend on that having happened: it names a fixed file beside its
    siblings, and only one revert runs at a time.
    """

    source = (ROOT / "appliance" / "manager_install.py").read_text(encoding="utf-8")
    body = source.split("def prepare_revert(", 1)[1].split("\ndef ", 1)[0]

    assert "REVERT_STAGING_NAME" in body
    # The identifier may travel as a value into the record and the request.
    # What it may not do is appear in a path expression.
    for line in body.splitlines():
        if "Path(" in line or "packages_dir" in line:
            assert "build_id" not in line, line.strip()


INSTALLER = Path(__file__).resolve().parents[1] / "packaging" / "appliance" / "bin" / "install-manager.sh"


def _restore_function():
    """The shipped restore, lifted out of the real installer script."""

    match = re.search(
        r"^ARMED_REVERTER=.*?^restore_armed_reverter\(\) \{.*?^\}$",
        INSTALLER.read_text(encoding="utf-8"),
        re.DOTALL | re.MULTILINE,
    )
    assert match, "install-manager.sh no longer defines restore_armed_reverter()"
    return match.group(0)


def _trap_lines():
    """The shipped trap wiring, lifted out of the real installer script."""

    lines = [
        line
        for line in INSTALLER.read_text(encoding="utf-8").splitlines()
        if line.startswith("trap ")
    ]
    assert lines, "install-manager.sh no longer wires restore_armed_reverter to a trap"
    return "\n".join(lines)


def test_the_installer_rearms_the_reverter_whatever_dpkg_put_on(tmp_path):
    """A revert installs an older package, and an older postinst has no fix.

    The deadline that a revert arms has to be runnable afterwards, so the
    manager driving the install restores the bit itself rather than trusting
    the package it just put on to have left it alone.
    """

    state = tmp_path / "packages"
    state.mkdir()
    reverter = state / manager_verify.REVERTER_NAME
    reverter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    reverter.chmod(0o600)  # what an unpatched postinst leaves behind

    script = "\n".join(
        ["set -eu", f'STATE="{state}"', _restore_function(), "restore_armed_reverter"]
    )
    subprocess.run(["sh", "-c", script], check=True, timeout=60)

    assert stat.S_IMODE(reverter.stat().st_mode) == manager_verify.REVERTER_MODE
    assert os.access(reverter, os.X_OK)


def test_the_installer_restores_on_every_exit_including_a_failed_revert(tmp_path):
    """A revert that fails is exactly when the deadline is the last way out.

    That path leaves by ``fail``, which no call placed after a dpkg reaches, so
    the restore hangs off EXIT. Proven by running the real wiring and leaving
    through the failing path rather than by counting call sites.
    """

    state = tmp_path / "packages"
    state.mkdir()
    reverter = state / manager_verify.REVERTER_NAME
    reverter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    reverter.chmod(0o600)

    text = INSTALLER.read_text(encoding="utf-8")
    assert "trap restore_armed_reverter EXIT" in text, text

    script = "\n".join(
        [
            "set -eu",
            f'STATE="{state}"',
            _restore_function(),
            "trap restore_armed_reverter EXIT",
            # stand in for `fail revert_failed`: a non-zero exit taken without
            # ever reaching a call site placed after a dpkg.
            "exit 1",
        ]
    )
    result = subprocess.run(["sh", "-c", script], timeout=60)
    assert result.returncode == 1

    assert stat.S_IMODE(reverter.stat().st_mode) == manager_verify.REVERTER_MODE
    assert os.access(reverter, os.X_OK)


def test_the_installer_restores_when_systemd_times_the_unit_out(tmp_path):
    """The install unit has a 900 s TimeoutStartSec, so SIGTERM is a real exit.

    dash does not run an EXIT trap on a signal, and the revert path has already
    put an older package's postinst through the agent tree by then. Without a
    signal trap the deadline armed for that revert cannot run at all.
    """

    state = tmp_path / "packages"
    state.mkdir()
    reverter = state / manager_verify.REVERTER_NAME
    reverter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    reverter.chmod(0o600)

    started = tmp_path / "started"
    script = "\n".join(
        [
            "set -eu",
            f'STATE="{state}"',
            _restore_function(),
            _trap_lines(),
            f': > "{started}"',
            # Short sleeps, not one long one: a shell defers a signal trap
            # until the foreground command returns, so `sleep 30` would make
            # this test measure its own patience instead of the restore.
            "while :; do sleep 0.05; done",
        ]
    )
    process = subprocess.Popen(["sh", "-c", script])
    try:
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists(), "the probe never started"
        process.terminate()
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert stat.S_IMODE(reverter.stat().st_mode) == manager_verify.REVERTER_MODE
    assert os.access(reverter, os.X_OK)


def test_the_installer_names_the_reverter_the_manager_arms():
    """Shell cannot import the constant, so the duplicated name is held to it."""

    assert manager_verify.REVERTER_NAME in INSTALLER.read_text(encoding="utf-8")


# --- the installer as the unit runs it ---------------------------------------


def installer_under(state):
    """The shipped installer, pointed at a directory a test can write.

    Everything but the one ``STATE=`` assignment runs verbatim; that the shipped
    value is the directory the agent writes is pinned separately by
    ``test_the_installer_reads_the_directory_the_agent_writes``.
    """

    text = INSTALLER.read_text(encoding="utf-8")
    patched, count = re.subn(
        r"^STATE=.*$", f'STATE="{state}"', text, count=1, flags=re.MULTILINE
    )
    assert count == 1, "install-manager.sh no longer assigns STATE on one line"
    script = state / "install-manager-under-test.sh"
    script.write_text(patched, encoding="utf-8")
    return script


def fake_dpkg(directory, *, status, version, install_fails, configure=None, previous_ok=True):
    """A dpkg whose idea of what is installed a test can move.

    ``status``/``version`` are what dpkg-query answers; ``configure`` is what
    ``dpkg --configure -a`` leaves behind, as ``(exit, status, version)``.
    """

    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "calls.log"
    db = directory / "dpkg-db"
    db.write_text(f"{status}|{version}\n", encoding="utf-8")

    configure_exit, configure_status, configure_version = configure or (0, status, version)
    (directory / "dpkg").write_text(
        "#!/bin/sh\n"
        f'echo "dpkg $*" >> "{log}"\n'
        'case "$*" in\n'
        "  *--configure*)\n"
        f'    printf "%s|%s\\n" "{configure_status}" "{configure_version}" > "{db}"\n'
        f"    exit {configure_exit} ;;\n"
        "  *previous.deb*)\n"
        f'    printf "%s|%s\\n" installed 0.1.0 > "{db}"\n'
        f"    exit {0 if previous_ok else 1} ;;\n"
        "  *--install*)\n"
        f"    exit {1 if install_fails else 0} ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (directory / "dpkg-query").write_text(
        "#!/bin/sh\n"
        f'echo "dpkg-query $*" >> "{log}"\n'
        "fmt=\n"
        "want=\n"
        'for arg in "$@"; do\n'
        '  if [ "$want" = yes ]; then fmt=$arg; want=; continue; fi\n'
        '  [ "$arg" = "-f" ] && want=yes\n'
        "done\n"
        f'state=$(cut -d"|" -f1 < "{db}")\n'
        f'known=$(cut -d"|" -f2 < "{db}")\n'
        'printf %s "$fmt" | sed -e "s/[$]{Version}/$known/g" '
        '-e "s/[$]{db:Status-Status}/$state/g"\n',
        encoding="utf-8",
    )
    for name in ("dpkg", "dpkg-query"):
        (directory / name).chmod(0o755)
    return log


def run_installer(state, tools):
    environment = dict(os.environ)
    environment["PATH"] = f"{tools}:{environment['PATH']}"
    return subprocess.run(
        ["sh", str(installer_under(state))],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )


def staged(state, *, version="0.2.0", previous=True):
    state.mkdir(parents=True, exist_ok=True)
    archive = state / "current.deb"
    archive.write_bytes(b"the package being installed")
    if previous:
        (state / "previous.deb").write_bytes(b"the package that was running")
    (state / manager_install.REQUEST_NAME).write_text(
        json.dumps({"archive": str(archive), "version": version, "build_id": "b", "sha256": "x"}),
        encoding="utf-8",
    )
    return archive


def installer_outcome(state):
    return json.loads((state / manager_install.RESULT_NAME).read_text(encoding="utf-8"))


def test_an_install_that_dpkg_completes_on_the_second_pass_is_not_undone(tmp_path):
    """`dpkg --configure -a` is a cure, and a cure that worked is not a failure.

    A postinst that times out under load leaves the new package half-configured;
    the second pass finishes it, and the appliance is then running exactly what
    the operator asked for. Reinstalling the previous package from there throws
    a healthy update away, restarts both services twice more, and files the run
    as `reverted`.
    """

    state = tmp_path / "packages"
    staged(state, version="0.2.0")
    tools = tmp_path / "tools"
    log = fake_dpkg(
        tools,
        status="installed",
        version="0.1.0",
        install_fails=True,
        configure=(0, "installed", "0.2.0"),
    )

    result = run_installer(state, tools)

    assert "previous.deb" not in log.read_text(encoding="utf-8"), "a healthy install was undone"
    assert installer_outcome(state)["outcome"] == manager_install.OUTCOME_INSTALLED
    assert result.returncode == 0, result.stderr


def test_a_configure_that_reports_success_without_installing_still_reverts(tmp_path):
    """An install that failed before unpacking leaves nothing to configure.

    `dpkg --configure -a` then exits 0 having done nothing, so its exit code
    cannot stand in for the question -- what is installed has to be read back.
    """

    state = tmp_path / "packages"
    staged(state, version="0.2.0")
    tools = tmp_path / "tools"
    log = fake_dpkg(
        tools,
        status="installed",
        version="0.1.0",
        install_fails=True,
        configure=(0, "installed", "0.1.0"),
    )

    run_installer(state, tools)

    assert "previous.deb" in log.read_text(encoding="utf-8")
    assert installer_outcome(state)["outcome"] == manager_install.OUTCOME_REVERTED


def test_a_half_configured_package_at_the_new_version_is_not_taken_for_installed(tmp_path):
    """Unpacked at the right version is not the same as installed."""

    state = tmp_path / "packages"
    staged(state, version="0.2.0")
    tools = tmp_path / "tools"
    log = fake_dpkg(
        tools,
        status="installed",
        version="0.1.0",
        install_fails=True,
        configure=(1, "half-configured", "0.2.0"),
    )

    run_installer(state, tools)

    assert "previous.deb" in log.read_text(encoding="utf-8")
    assert installer_outcome(state)["outcome"] == manager_install.OUTCOME_REVERTED


def test_an_install_dpkg_accepts_outright_is_recorded_without_a_second_pass(tmp_path):
    state = tmp_path / "packages"
    staged(state, version="0.2.0")
    tools = tmp_path / "tools"
    log = fake_dpkg(tools, status="installed", version="0.1.0", install_fails=False)

    result = run_installer(state, tools)

    assert result.returncode == 0, result.stderr
    assert installer_outcome(state)["outcome"] == manager_install.OUTCOME_INSTALLED
    assert "--configure" not in log.read_text(encoding="utf-8")


def test_the_installer_asks_dpkg_the_question_manager_verify_names():
    """Both scripts read one fact; only one place decides how it is asked."""

    script = INSTALLER.read_text(encoding="utf-8")
    assert f"-f '{manager_verify.DPKG_STATE_QUERY}'" in script
    assert f'"{manager_verify.DPKG_STATE_INSTALLED}|$WANTED"' in script
