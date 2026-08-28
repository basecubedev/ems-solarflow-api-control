# SPDX-License-Identifier: AGPL-3.0-or-later
"""The way back from the first update, which no card had.

``manager_retention.retain`` grew a ``rotate=False`` mode whose docstring names
its caller -- "what an image build does for a manager that was never installed
through this path". Nothing called it. ``rotate=False`` appeared in four test
modules and in no production file, so the mode existed and the appliance it was
written for never used it.

The consequence is on the first update of every flashed card and nowhere else,
which is why it survived. dpkg keeps no copy of the archive it installed from,
so a card whose manager arrived in the image holds nothing to retain. The first
update rotates that nothing into ``previous``, ``manager_verify`` finds
``revert_unavailable`` when its deadline expires, and the console is gone with no
way back -- on the one path whose entire purpose is being a way back, and against
``docs/appliance/adr/manager-self-update.md``, which says the outgoing package is
retained before anything is unpacked.

The image now seeds the record while the archive is still in hand. These tests
pin the behaviour that seeding buys, and the layer step that buys it.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from appliance import manager_retention as retention

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
LAYER = ROOT / "packaging" / "appliance" / "image" / "layer" / "ems-appliance.yaml"


class FakePaths:
    def __init__(self, root):
        self.packages_dir = Path(root) / "packages"


@pytest.fixture
def paths(tmp_path):
    return FakePaths(tmp_path)


def archive(tmp_path, name, body):
    target = tmp_path / name
    target.write_bytes(body)
    return target


def seed_image_manager(paths, tmp_path):
    """What the image build now does, at the point dpkg has run."""

    from appliance import manager_install

    return manager_install.seed_installed(
        paths,
        archive(tmp_path, "image.deb", b"the manager the card was flashed with"),
        sha256="sha256:" + "a" * 64,
        version="0.1.0",
        architecture="arm64",
    )


def test_the_first_update_can_go_back_to_the_manager_the_image_carried(paths, tmp_path):
    """The whole point. Before the seed this appliance had nothing to revert to
    at the exact moment it first needed one."""

    seed_image_manager(paths, tmp_path)

    retention.retain(
        paths,
        archive(tmp_path, "next.deb", b"the update"),
        sha256="sha256:" + "b" * 64,
        version="0.2.0",
        architecture="arm64",
    )

    kept = retention.read(paths)
    assert kept.can_revert
    assert kept.previous.version == "0.1.0"
    assert retention.revert_target(paths).version == "0.1.0"


def test_the_archive_the_revert_reinstalls_is_really_on_disk(paths, tmp_path):
    """A record naming a file that is not there is what rollback-manager has
    always refused on."""

    seed_image_manager(paths, tmp_path)
    retention.retain(
        paths,
        archive(tmp_path, "next.deb", b"the update"),
        sha256="sha256:" + "b" * 64,
        version="0.2.0",
    )

    target = retention.revert_target(paths)

    assert Path(target.path).is_file()
    assert Path(target.path).read_bytes() == b"the manager the card was flashed with"


def test_seeding_is_refused_on_an_appliance_that_has_already_installed(paths, tmp_path):
    """Seeding is for a card that has never installed anything. On one that has,
    it would discard the retention chain it exists to protect, so it refuses
    rather than doing a quieter kind of damage."""

    from appliance import manager_install

    seed_image_manager(paths, tmp_path)

    with pytest.raises(manager_install.ManagerInstallError) as refusal:
        seed_image_manager(paths, tmp_path)

    assert refusal.value.code == "manager_already_retained"
    assert retention.read(paths).current.version == "0.1.0"


def test_only_one_module_writes_the_retention_record():
    """The console rollback, the browser update and the image seed are three
    callers of one owner, not three writers."""

    cli = (ROOT / "appliance" / "cli.py").read_text(encoding="utf-8")

    assert "manager_retention.retain(" not in cli
    assert "manager_install.seed_installed(" in cli


def test_the_image_seeds_the_record_before_it_deletes_the_archive():
    """Order is the whole of it: the archive is the only copy of what the card
    runs, and the layer removes it two lines later."""

    body = yaml.safe_load(LAYER.read_text(encoding="utf-8").split("# METAEND\n---\n", 1)[1])
    install = [
        hook
        for hook in body["mmdebstrap"]["customize-hooks"]
        if "dpkg -i /tmp/ems-appliance-manager.deb" in str(hook)
    ]
    assert len(install) == 1, "the package is installed in more than one place"
    hook = str(install[0])

    assert "retain-installed-manager" in hook
    assert hook.index("retain-installed-manager") < hook.index("rm -f"), (
        "the archive is deleted before it is retained, so nothing is kept"
    )


def test_the_cli_offers_the_command_the_image_calls():
    """The layer runs it in a chroot, where a missing subcommand is an argparse
    error two minutes into a three-hour build."""

    from appliance.cli import build_parser

    parser = build_parser()
    parsed = parser.parse_args(["retain-installed-manager", "--package", "/tmp/x.deb"])

    assert parsed.package == "/tmp/x.deb"
    assert parsed.handler.__name__ == "command_retain_installed_manager"


def build_deb(tmp_path, *, version, architecture="arm64"):
    """A real package, because the command reads it with dpkg-deb."""

    stage = tmp_path / "stage"
    (stage / "DEBIAN").mkdir(parents=True)
    (stage / "DEBIAN" / "control").write_text(
        "Package: ems-appliance-manager\n"
        f"Version: {version}\n"
        f"Architecture: {architecture}\n"
        "Maintainer: t <t@example.invalid>\n"
        "Description: test\n",
        encoding="utf-8",
    )
    archive = tmp_path / f"ems-appliance-manager_{version}_{architecture}.deb"
    subprocess.run(["dpkg-deb", "--build", str(stage), str(archive)],
                   check=True, capture_output=True, timeout=120)
    return archive


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="the command reads the .deb with dpkg-deb")
def test_the_command_the_image_runs_actually_seeds_the_record(tmp_path, monkeypatch):
    """Executed, not inspected.

    The first version of this asserted the subcommand was registered and that
    the layer called it in the right order, and both were true while the command
    could not run at all: it reaches for dpkg-deb, which was not on
    ``appliance/commands.py``'s allowlist, so ``CommandRunner`` refused before any
    subprocess. Under the layer hook's ``set -eu`` that failed the whole image
    build -- and every appliance test still passed.
    """

    from appliance import cli

    archive = build_deb(tmp_path, version="0.4.2")
    packages = tmp_path / "packages"
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "resolve_paths", lambda: FakePaths(tmp_path))

    code = cli.main(["retain-installed-manager", "--package", str(archive)])

    assert code == 0
    kept = retention.read(FakePaths(tmp_path))
    assert kept.current.present
    assert kept.current.version == "0.4.2", "the record does not describe the package installed"
    assert kept.current.architecture == "arm64"
    assert Path(kept.current.path).is_file()
    assert packages.exists()


def test_every_tool_the_appliance_runs_is_on_the_allowlist():
    """The class the above is one instance of.

    ``CommandRunner`` refuses an unlisted tool before it runs anything, so a new
    call site with a new binary fails only when that code path is exercised --
    which for an image-build hook means at build time, on a runner, after
    everything else has passed.
    """

    from appliance.commands import EXECUTABLES

    called = re.compile(r'\brun\(\s*"([a-z][a-z0-9.+-]*)"')
    # A module may bring its own runner instead of widening the agent's --
    # rpi_image_gen does exactly that for git, on purpose, because nothing in
    # the agent or the web API resolves a source tree. Such a module names its
    # tool in a <TOOL>_EXECUTABLES constant.
    own = re.compile(r"^([A-Z][A-Z0-9_]*)_EXECUTABLES\s*=", re.M)
    missing = {}
    for path in sorted((ROOT / "appliance").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        allowed = set(EXECUTABLES) | {
            name.lower().replace("_", "-") for name in own.findall(source)
        }
        for tool in called.findall(source):
            if tool not in allowed:
                missing.setdefault(tool, []).append(path.name)

    assert missing == {}, f"tools a CommandRunner would refuse: {missing}"
