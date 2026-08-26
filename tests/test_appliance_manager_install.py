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
from pathlib import Path

import pytest

from appliance import manager_install, manager_releases, manager_retention, persistent_state

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
